#!/usr/bin/env python3
"""selftest：stub 宿主下验证 v0.2「注入-排水」全链路（开发计划 §0.2/§0.3 + 对接契约）。

遵循 astrbot-plugin-dev skill references/09 的「stub 宿主自测法」：

- ``sys.path`` **先桩**（tests/stub 的假 astrbot 包，含 webchat_queue_mgr /
  active_event_registry / astrbot_path 三个宿主面桩）**后本体**（插件根目录，
  core/ 作为顶层包导入）；main.py 依赖真实宿主运行时注入，桩下不可导入，
  因此只测 core/；
- v0.2 覆盖点：
  - A：SSE 事件封装 / seq 单调 / 缓冲重放（幂等）；
  - B：uistore 只读归档层（读 / 分页 / list_archives / 清档）；
  - C：umo 构造、cid/conv_id 双主键（new_conversation 的 umo 与注入 umo 逐字符
    一致、get_conversation/update_conversation 经 conv_id）、会话 CRUD 映射宿主、
    历史映射（plain 拼接 / think→reasoning / tool_call parts→tools / agent_stats→
    usage / 媒体 parts→attachments 索引反查）、分页语义（page 1 最新、页内旧->新、
    has_more 配 count）、legacy 归档响应附 legacy:true、级联/legacy 删除分支；
  - D：生成内核「注入-排水」——注入三步形状（平台历史剥 path / back_queue /
    会话队列 tuple / flags（enable_streaming 回读宿主配置）/ checkpoint 同源）、
    翻译表逐型（text_delta / reasoning_delta / tool_call{start} /
    tool_result{result|error 互斥 + elapsed_ms} / agent_stats→stats 事件 +
    done.usage / 媒体帧→media 事件上屏 / end→bot 落库→done{message_id,usage}）、
    坏载荷防御（坏 JSON / 非 dict / message_id 不匹配 / 冗余帧不产事件）、
    空闲超时兜底（注入短超时）、remove_back_queue 纪律（正常 / 异常 / 取消 /
    shutdown）、stop 先落 stopped + request_agent_stop_all(umo) + 消费任务不被
    cancel 且部分回复落平台历史、single-flight 409 / 并发上限 409（配置热生效）、
    bot 媒体帧 db 注册 + 索引 + media 事件 + 助手 attachments（缺文件防御）、
    流式配置适配（read_streaming_response / config 端点透传）、并发 send 竞态
    （TOCTOU）、删除生成中会话（取消先行，已删历史不复活）；
  - J6：SSE 保活 ping（不占业务 seq）；
  - H：附件（宿主附件区落盘 + db 注册 + 索引 f_id↔attachment_id + 白名单 /
    防穿越 / 413 / 截断 / 孤儿清理）；
  - L：standalone 鉴权 / 限速 / token 纯逻辑；E/K：路由表精确注册、契约错误包、
    send→注入→排水→done 端到端、附件端点与级联删除链路；
  - F 组空态骨架保留。

用法（工作区任意目录均可）::

    .venv/bin/python.exe astrbot_plugin_webui/tests/selftest.py

退出码：0 全部通过；1 有失败项。沙盒目录 tests/.sandbox-<pid>（按进程隔离，
防并发运行互踩）每次运行前清空、结束（含失败）后删除；崩溃遗留的陈旧沙盒
（>1h）由下次运行清扫。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import shutil
import sys
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

TESTS_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = TESTS_DIR.parent
STUB_DIR = TESTS_DIR / "stub"
# 沙盒按进程隔离：门禁/CI 可能并发跑多份 selftest，固定路径会让进程 A 的
# fresh_sandbox()（rmtree）删掉进程 B 正在用的目录，表现为随机单点失败
# （如 H2-8 索引突然读空、D4-10 IndexError）。
SANDBOX = TESTS_DIR / f".sandbox-{os.getpid()}"
STALE_SANDBOX_MAX_AGE_SECONDS = 3600.0

sys.path.insert(0, str(STUB_DIR))  # 先桩：假 astrbot 包优先于任何真实安装
sys.path.insert(0, str(PLUGIN_ROOT))  # 后本体：core/ 作为顶层包导入

import core.attachment_service as attachment_service  # noqa: E402
import core.chat_service as chat_service  # noqa: E402
import core.conversation_service as conversation_service  # noqa: E402
import core.persona_service as persona_service  # noqa: E402
import core.pipeline_bridge as pipeline_bridge  # noqa: E402
import core.routes as routes  # noqa: E402
import core.sse as sse  # noqa: E402
import core.standalone as standalone  # noqa: E402
import core.uistore as uistore  # noqa: E402
from astrbot.api.web import (  # noqa: E402
    PluginUploadFile,
    request as stub_request,
)

# 预期异常场景（D5/D6/J3/J4 的 provider 炸裂、H2 损坏文件、J3 降级阶梯等）会经
# astrbot.api.logger 输出 ERROR/WARNING + traceback 到 stderr——这是被测代码的
# 正常日志行为，但会让门禁把 stderr 噪声误判为失败；此处压掉桩 logger（level
# 判定在记录创建前生效），断言结果走 stdout 的 check()，不受影响。
logging.getLogger("astrbot.stub").setLevel(logging.CRITICAL)
logging.getLogger("astrbot.stub").addHandler(logging.NullHandler())

FAILURES: list[str] = []
PASSED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    """记录单条断言结果。"""
    global PASSED
    if cond:
        PASSED += 1
    else:
        FAILURES.append(f"{name}{('：' + detail) if detail else ''}")
        print(f"  FAIL  {name}{('：' + detail) if detail else ''}")


def fresh_sandbox() -> Path:
    """清空并返回沙盒数据目录（每次场景独立；目录按进程 PID 隔离）。"""
    shutil.rmtree(SANDBOX, ignore_errors=True)
    SANDBOX.mkdir(parents=True, exist_ok=True)
    return SANDBOX


def sweep_stale_sandboxes(max_age_seconds: float = STALE_SANDBOX_MAX_AGE_SECONDS) -> None:
    """清扫崩溃运行遗留的陈旧沙盒目录（>max_age_seconds；不触碰并发运行的活跃沙盒）。"""
    now = datetime.now(timezone.utc)
    for path in TESTS_DIR.glob(".sandbox*"):
        if path == SANDBOX:
            continue
        try:
            age = (now - datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)).total_seconds()
        except OSError:
            continue
        if age > max_age_seconds:
            shutil.rmtree(path, ignore_errors=True)


# ---------------------------------------------------------------------------
# 桩宿主：假 db / 会话与历史管理器 / persona_manager / context / plugin
# （v0.2 注入-排水内核：webchat_queue_mgr / active_event_registry / astrbot_path
# 三个宿主面由 tests/stub 桩模块提供；D 组用例以脚本化载荷驱动排水翻译）
# ---------------------------------------------------------------------------


class FakeConversationManager:
    """stub：conversation_manager（内存会话表 + 调用记录；v0.2 双主键语义）。"""

    def __init__(self) -> None:
        self.convs: dict[str, SimpleNamespace] = {}
        self.session_conversations: dict[str, str] = {}
        self.title_updates: list[tuple[str, str]] = []
        self.pairs: list[tuple[str, dict, dict]] = []
        self.deleted: list[str] = []
        self.deleted_by_user: list[str] = []
        self.new_calls: list[str] = []  # new_conversation 收到的 umo
        self.get_calls: list[tuple[str, str]] = []  # get_conversation 收到的 (umo, conversation_id)
        self.update_calls: list[tuple[str, str | None, str | None]] = []  # (umo, conversation_id, title)

    def seed(self, cid: str, persona_id: str | None = None, updated_at: int = 1000) -> SimpleNamespace:
        conv = SimpleNamespace(
            platform_id="webchat",
            user_id=conversation_service.build_umo(cid),
            cid=cid,
            history="[]",
            title="新会话",
            persona_id=persona_id,
            created_at=updated_at,
            updated_at=updated_at,
            token_usage=0,
        )
        self.convs[cid] = conv
        return conv

    async def new_conversation(self, unified_msg_origin, platform_id=None, content=None, title=None, persona_id=None):
        self.new_calls.append(unified_msg_origin)
        cid = uuid.uuid4().hex
        conv = self.seed(cid, persona_id=persona_id)
        conv.user_id = unified_msg_origin
        conv.title = title or "新会话"
        self.session_conversations[unified_msg_origin] = cid  # 宿主 new_conversation 自动设当前
        return cid

    async def get_curr_conversation_id(self, unified_msg_origin):
        return self.session_conversations.get(unified_msg_origin)

    async def get_conversations(self, unified_msg_origin=None, platform_id=None):
        return [
            conv
            for conv in self.convs.values()
            if platform_id is None or conv.platform_id == platform_id
        ]

    async def get_conversation(self, unified_msg_origin, conversation_id, create_if_not_exists=False):
        self.get_calls.append((unified_msg_origin, conversation_id))
        return self.convs.get(conversation_id)

    async def delete_conversation(self, unified_msg_origin, conversation_id=None):
        if conversation_id:
            self.convs.pop(conversation_id, None)
            self.deleted.append(conversation_id)

    async def delete_conversations_by_user_id(self, unified_msg_origin):
        for cid in [
            conv.cid for conv in self.convs.values() if conv.user_id == unified_msg_origin
        ]:
            self.convs.pop(cid, None)
            self.deleted.append(cid)
        self.deleted_by_user.append(unified_msg_origin)
        self.session_conversations.pop(unified_msg_origin, None)

    async def update_conversation(self, unified_msg_origin, conversation_id=None, history=None, title=None, persona_id=None, token_usage=None):
        self.update_calls.append((unified_msg_origin, conversation_id, title))
        conv = self.convs.get(conversation_id)
        if conv is None:
            return
        if title is not None:
            conv.title = title
            self.title_updates.append((conversation_id, title))
        if history is not None:
            conv.history = json.dumps(history)

    async def add_message_pair(self, cid, user_message, assistant_message):
        conv = self.convs.get(cid)
        if conv is None:
            raise Exception(f"Conversation with id {cid} not found")
        history = json.loads(conv.history or "[]")
        history.append(user_message)
        history.append(assistant_message)
        conv.history = json.dumps(history)
        self.pairs.append((cid, user_message, assistant_message))


class FakeDb:
    """stub：宿主 BaseDatabase（platform_sessions / attachments 最小面）。"""

    def __init__(self) -> None:
        self.platform_sessions: dict[str, SimpleNamespace] = {}
        self.attachments: dict[str, SimpleNamespace] = {}
        self._attachment_seq = 0
        self.deleted_sessions: list[str] = []
        self.deleted_attachments: list[str] = []

    async def create_platform_session(self, creator, platform_id="webchat", session_id=None, display_name=None, is_group=0):
        session = SimpleNamespace(
            session_id=session_id or uuid.uuid4().hex,
            creator=creator,
            platform_id=platform_id,
            display_name=display_name,
            is_group=is_group,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        self.platform_sessions[session.session_id] = session
        return session

    async def get_platform_session_by_id(self, session_id):
        return self.platform_sessions.get(session_id)

    async def get_platform_sessions_by_creator(self, creator, platform_id=None, page=1, page_size=20):
        sessions = [
            s for s in self.platform_sessions.values()
            if s.creator == creator and (platform_id is None or s.platform_id == platform_id)
        ]
        sessions.sort(key=lambda s: str(s.updated_at), reverse=True)
        return [{"session": s, "project_id": None, "project_title": None, "project_emoji": None} for s in sessions]

    async def delete_platform_session(self, session_id):
        self.platform_sessions.pop(session_id, None)
        self.deleted_sessions.append(session_id)

    async def insert_attachment(self, path, type, mime_type):
        self._attachment_seq += 1
        attachment = SimpleNamespace(
            attachment_id=f"att_{self._attachment_seq:06d}",
            path=str(path),
            type=type,
            mime_type=mime_type,
        )
        self.attachments[attachment.attachment_id] = attachment
        return attachment

    async def get_attachment_by_id(self, attachment_id):
        return self.attachments.get(attachment_id)

    async def delete_attachment(self, attachment_id):
        existed = self.attachments.pop(attachment_id, None) is not None
        if existed:
            self.deleted_attachments.append(attachment_id)
        return existed


class FakeHistoryManager:
    """stub：PlatformMessageHistoryManager（内存行，page 1 最新语义与宿主一致）。"""

    def __init__(self) -> None:
        self.rows: list[SimpleNamespace] = []
        self._seq = 0
        self.inserts: list[dict] = []
        self.deleted_offsets: list[dict] = []

    async def insert(self, platform_id, user_id, content, sender_id=None, sender_name=None, llm_checkpoint_id=None, max_messages=None):
        self._seq += 1
        row = SimpleNamespace(
            id=self._seq,
            platform_id=platform_id,
            user_id=user_id,
            content=content,
            sender_id=sender_id,
            sender_name=sender_name,
            llm_checkpoint_id=llm_checkpoint_id,
            created_at=datetime.now(timezone.utc),
        )
        self.rows.append(row)
        self.inserts.append({"content": content, "sender_id": sender_id, "checkpoint": llm_checkpoint_id})
        return row

    async def get(self, platform_id, user_id, page=1, page_size=200):
        matched = [r for r in self.rows if r.platform_id == platform_id and r.user_id == user_id]
        matched.sort(key=lambda r: r.id, reverse=True)  # 宿主 db desc(id)
        offset = (page - 1) * page_size
        page_rows = matched[offset:offset + page_size]
        return list(reversed(page_rows))  # 宿主 manager.get 反转为页内旧->新

    async def count(self, platform_id, user_id):
        return len([r for r in self.rows if r.platform_id == platform_id and r.user_id == user_id])

    async def delete(self, platform_id, user_id, offset_sec=86400):
        self.deleted_offsets.append({"platform_id": platform_id, "user_id": user_id, "offset_sec": offset_sec})
        self.rows = [r for r in self.rows if not (r.platform_id == platform_id and r.user_id == user_id)]


class FakePersonaManager:
    """stub：persona_manager（Persona PO 属性访问 + v3 Personality dict 访问）。"""

    def __init__(self, personas: list[SimpleNamespace], default_name: str) -> None:
        self.personas = list(personas)
        self.default_name = default_name

    async def get_all_personas(self):
        return list(self.personas)

    async def get_persona(self, persona_id):
        for persona in self.personas:
            if persona.persona_id == persona_id:
                return persona
        raise ValueError(f"Persona with ID {persona_id} does not exist.")

    def get_persona_v3_by_id(self, persona_id):
        for persona in self.personas:
            if persona.persona_id == persona_id:
                return self._v3(persona)
        return None

    async def get_default_persona_v3(self, umo=None):
        persona = next(
            (p for p in self.personas if p.persona_id == self.default_name), None
        )
        return self._v3(persona) if persona else {"name": self.default_name, "prompt": "", "tools": None}

    @staticmethod
    def _v3(persona: SimpleNamespace) -> dict:
        return {
            "name": persona.persona_id,
            "prompt": persona.system_prompt,
            "tools": persona.tools,
            "_begin_dialogs_processed": [],
        }


class FakeContext:
    """stub：宿主上下文（捕获 register_web_api 路由表 + db/历史/会话管理器）。"""

    def __init__(self) -> None:
        self.routes: list[tuple[str, object, list[str], str]] = []
        self.conversation_manager = FakeConversationManager()
        self.message_history_manager = FakeHistoryManager()
        self.persona_manager = FakePersonaManager([], "default")
        self._stars = {"astrbot_plugin_webui": SimpleNamespace(version="v9.9.9-test")}
        self.db = FakeDb()

    def get_db(self) -> FakeDb:
        return self.db

    def register_web_api(self, route, view_handler, methods, desc) -> None:
        self.routes.append((route, view_handler, list(methods), desc))

    def get_registered_star(self, name):
        return self._stars.get(name)


class FakePlugin:
    """stub：插件实例（config dict + 沙盒 data_dir）。"""

    def __init__(self, data_dir: Path, config: dict | None = None) -> None:
        self.name = "astrbot_plugin_webui"
        self.config = dict(config or {})
        self.context = FakeContext()
        self._data_dir = Path(data_dir)

    def data_dir(self) -> Path:
        self._data_dir.mkdir(parents=True, exist_ok=True)
        return self._data_dir


def _schema_defaults() -> dict:
    """从 _conf_schema.json 读默认值（配置键恒存在的前提；单一事实源 = schema）。"""
    import json as _json

    schema = _json.loads(
        (PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8")
    )
    return {key: item.get("default") for key, item in schema.items()}


def make_plugin(sandbox: Path, config: dict | None = None) -> FakePlugin:
    base = _schema_defaults()  # 代码不再有第二重默认值兜底，测试配置必须含全部键
    base.update(config or {})
    plugin = FakePlugin(sandbox, base)
    plugin.context.persona_manager = FakePersonaManager(
        [
            SimpleNamespace(persona_id="p1", system_prompt="你是测试人格", begin_dialogs=[], tools=None),
            SimpleNamespace(persona_id="p2", system_prompt="x" * 200, begin_dialogs=[], tools=[]),
            SimpleNamespace(persona_id="p3", system_prompt="带工具", begin_dialogs=[], tools=["t1", "t2"]),
        ],
        "p1",
    )
    return plugin


async def wait_for(cond, timeout: float = 2.0, interval: float = 0.01) -> bool:
    """轮询等待条件成立（测试辅助，不改全局时钟）。"""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if cond():
            return True
        await asyncio.sleep(interval)
    return cond()


def _stub_queue_mgr():
    """桩 webchat_queue_mgr 单例（tests/stub 桩模块；脚本化载荷驱动排水翻译）。"""
    from astrbot.core.platform.sources.webchat.webchat_queue_mgr import webchat_queue_mgr

    return webchat_queue_mgr


def _stub_registry():
    """桩 active_event_registry 单例（记录 request_agent_stop_all 调用）。"""
    from astrbot.core.utils.active_event_registry import active_event_registry

    return active_event_registry


def _cfg_ctx(config):
    """构造带 get_config 的最小 context（read_streaming_response 断言用）。"""
    return SimpleNamespace(get_config=lambda: config)


def set_stub_host_data(sandbox: Path) -> Path:
    """把桩宿主数据目录指到沙盒内（附件落点 = <sandbox>/hostdata/attachments/）。"""
    from astrbot.core.utils import astrbot_path as stub_astrbot_path

    hostdata = sandbox / "hostdata"
    stub_astrbot_path.set_stub_data_path(hostdata)
    return hostdata


async def feed_frames(gen_id: str, frames: list[dict], interval: float = 0.01) -> None:
    """把脚本化载荷按序投进桩 back_queue（模拟宿主管道输出；间隔避免空闲超时误触发）。"""
    mgr = _stub_queue_mgr()
    for frame in frames:
        await asyncio.sleep(interval)
        await mgr.put_back_queue(gen_id, frame)


# ---------------------------------------------------------------------------
# A. sse.py：事件封装 / seq / 缓冲重放（幂等）
# ---------------------------------------------------------------------------


def test_sse() -> None:
    print("== A. sse 事件封装与缓冲 ==")
    frame = sse.encode_event("text_delta", {"seq": 0, "delta": "你"})
    check("A1 encode_event 帧格式", frame == 'event: text_delta\ndata: {"seq": 0, "delta": "你"}\n\n', repr(frame))
    try:
        sse.encode_event("unknown", {"seq": 0})
        check("A2 未知事件类型拒绝", False)
    except ValueError:
        check("A2 未知事件类型拒绝", True)
    try:
        sse.encode_event("text_delta", {})
        check("A3 缺 seq 拒绝", False)
    except ValueError:
        check("A3 缺 seq 拒绝", True)
    check("A4 stopped 属于事件类型", "stopped" in sse.EVENT_TYPES)
    check("A5 终态事件", set(sse.TERMINAL_EVENTS) == {"done", "stopped"})

    buffer = sse.new_buffer()
    check("A6 new_buffer 返回 deque", isinstance(buffer, deque))
    b1 = sse.buffer_append(buffer, "text_delta", {"delta": "a"})
    b2 = sse.buffer_append(buffer, "reasoning_delta", {"delta": "b"})
    b3 = sse.buffer_append(buffer, "done", {"message_id": "m_x", "usage": {"output": 1, "total": 2}})
    check("A7 seq 从 0 递增", (b1["seq"], b2["seq"], b3["seq"]) == (0, 1, 2))
    replay_full = sse.buffer_replay(buffer)
    check("A8 全量重放 3 条", len(replay_full) == 3)
    check("A9 重放幂等（两次结果一致）", replay_full == sse.buffer_replay(buffer))
    # after_seq = 期望收到的下一个 seq：0 全量；1 从 seq=1 续传
    check("A10 after_seq 续传过滤", [i["data"]["seq"] for i in sse.buffer_replay(buffer, 1)] == [1, 2])
    check("A11 条目形状 event+data", replay_full[0]["event"] == "text_delta" and replay_full[0]["data"]["delta"] == "a")
    empty = sse.new_buffer()
    check("A12 空缓冲重放为空列表", sse.buffer_replay(empty) == [])


# ---------------------------------------------------------------------------
# B. uistore.py：v0.2 降级只读归档层（读/分页/list_archives/清档）
# ---------------------------------------------------------------------------


def _write_archive(sandbox: Path, cid: str, messages: list[dict], updated_at: str | None = None) -> Path:
    """直接落一份归档文档（uistore 已无写 API，fixture 自备）。"""
    root = sandbox / "ui_history"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{cid}.json"
    doc = {
        "version": uistore.SCHEMA_VERSION,
        "cid": cid,
        "updated_at": updated_at or uistore.now_iso(),
        "messages": messages,
    }
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return path


async def _test_uistore() -> None:
    print("== B. uistore 只读归档层 ==")
    sandbox = fresh_sandbox()
    cid = uuid.uuid4().hex

    path = _write_archive(
        sandbox, cid,
        [
            {"id": "m_a", "role": "user", "text": "你好"},
            {"id": "m_b", "role": "assistant", "text": "回复", "usage": {"output": 3, "total": 9}},
            {"id": "m_c", "role": "user", "text": "msg1"},
            {"id": "m_d", "role": "user", "text": "msg2"},
        ],
    )
    check("B1 每 cid 一文件", path.is_file(), str(path))

    loaded = await uistore.load_conversation(sandbox, cid)
    check("B2 load 一致", [m["id"] for m in loaded["messages"]] == ["m_a", "m_b", "m_c", "m_d"])

    page1 = await uistore.page_messages(sandbox, cid, page=1, size=2)
    check("B3 page=1 为最新一页", [m["text"] for m in page1["messages"]] == ["msg1", "msg2"], str([m["text"] for m in page1["messages"]]))
    check("B4 total/size/page", (page1["total"], page1["size"], page1["page"]) == (4, 2, 1))
    check("B5 has_more=True", page1["has_more"] is True)
    check("B6 历史响应契约键", set(page1.keys()) == {"cid", "page", "size", "total", "has_more", "messages"})
    page3 = await uistore.page_messages(sandbox, cid, page=2, size=2)
    check("B7 最后一页含最早消息", [m["text"] for m in page3["messages"]] == ["你好", "回复"] and page3["has_more"] is False)
    page9 = await uistore.page_messages(sandbox, cid, page=9, size=2)
    check("B8 超页为空", page9["messages"] == [])

    # 迁移预留：未知版本尽力读取
    other = uuid.uuid4().hex
    legacy = sandbox / "ui_history" / f"{other}.json"
    legacy.write_text(json.dumps({"version": 0, "cid": other, "messages": [{"role": "user", "text": "hi"}]}, ensure_ascii=False), encoding="utf-8")
    migrated = await uistore.load_conversation(sandbox, other)
    check("B9 未知版本尽力读取", len(migrated["messages"]) == 1 and migrated["messages"][0]["text"] == "hi")

    # 损坏兜底：不清空用户数据
    broken = sandbox / "ui_history" / f"{uuid.uuid4().hex}.json"
    broken.write_text("not-json{{{", encoding="utf-8")
    doc_broken = await uistore.load_conversation(sandbox, broken.stem)
    check("B10 损坏兜底为空结构", doc_broken["messages"] == [])
    check("B11 损坏文件保留", broken.is_file() and broken.read_text(encoding="utf-8") == "not-json{{{")
    check("B12 缺失会话页为空", (await uistore.page_messages(sandbox, uuid.uuid4().hex))["total"] == 0)

    all_docs = await uistore.load_ui_history(sandbox)
    check("B13 全量扫描（损坏文件跳过）", set(all_docs["conversations"].keys()) >= {cid, other}, str(set(all_docs["conversations"].keys())))

    archives = await uistore.list_archives(sandbox)
    by_cid = {item["cid"]: item for item in archives}
    check(
        "B14 list_archives 归档项",
        set(by_cid.keys()) >= {cid, other}
        and by_cid[cid]["first_user_text"] == "你好"
        and by_cid[other]["first_user_text"] == "hi",
        str(archives),
    )

    await uistore.clear_conversation(sandbox, cid)
    check("B15 clear 删除归档文件", not path.exists())

    # ── v0.3.1 UI 设置与头像持久化 ──
    import base64 as _base64

    check("B16 设置缺省为空", await uistore.load_ui_settings(sandbox) == {})
    saved = await uistore.save_ui_settings(sandbox, {"compact": True, "flowWidth": "wide"})
    check("B17 save 返回快照", saved == {"compact": True, "flowWidth": "wide"})
    check("B18 load 往返", await uistore.load_ui_settings(sandbox) == {"compact": True, "flowWidth": "wide"})
    check("B19 设置文件名", uistore.ui_settings_file(sandbox).name == "ui_settings.json" and uistore.ui_settings_file(sandbox).is_file())
    check(
        "B20 头像文件名映射",
        uistore.avatar_file(sandbox, "avatarUser").name == "User_head.png"
        and uistore.avatar_file(sandbox, "avatarAssistant").name == "LLM_head.png",
    )
    try:
        uistore.avatar_file(sandbox, "evil/../x")
        check("B21 非法头像键拒绝", False)
    except ValueError:
        check("B21 非法头像键拒绝", True)
    check("B22 缺头像读为空", await uistore.read_avatar_data_url(sandbox, "avatarUser") == "")

    png_bytes = bytes.fromhex("89504e470d0a1a0a")  # PNG 魔数前 8 字节
    data_url = "data:image/png;base64," + _base64.b64encode(png_bytes).decode("ascii")
    await uistore.write_avatar_data_url(sandbox, "avatarUser", data_url)
    check("B23 头像落盘 User_head.png", uistore.avatar_file(sandbox, "avatarUser").is_file())
    check("B24 头像读回 data URL 一致", await uistore.read_avatar_data_url(sandbox, "avatarUser") == data_url)
    await uistore.write_avatar_data_url(sandbox, "avatarUser", data_url)
    check("B25 同内容幂等写（无 tmp 残留）", list(sandbox.glob("*.tmp")) == [], str(list(sandbox.glob('*.tmp'))))
    new_url = "data:image/png;base64," + _base64.b64encode(png_bytes + b"v2").decode("ascii")
    await uistore.write_avatar_data_url(sandbox, "avatarUser", new_url)
    check("B26 新头像覆盖旧文件", await uistore.read_avatar_data_url(sandbox, "avatarUser") == new_url)
    for bad in ["data:image/jpeg;base64,AAAA", "data:image/png;base64,!!!", "data:image/png;base64,", "hello"]:
        try:
            await uistore.write_avatar_data_url(sandbox, "avatarAssistant", bad)
            check(f"B27 非法 data URL 拒绝（{bad[:26]}…）", False)
        except ValueError:
            check(f"B27 非法 data URL 拒绝（{bad[:26]}…）", True)
    await uistore.clear_avatar(sandbox, "avatarUser")
    check("B28 clear 删除头像文件", not uistore.avatar_file(sandbox, "avatarUser").exists() and await uistore.read_avatar_data_url(sandbox, "avatarUser") == "")
    await uistore.clear_avatar(sandbox, "avatarUser")
    check("B29 clear 幂等", True)


# ---------------------------------------------------------------------------
# C. conversation_service.py：umo / 双主键 / CRUD 映射宿主 / 历史读取
# ---------------------------------------------------------------------------


async def _test_conversation_service() -> None:
    print("== C. conversation_service umo 与双主键 CRUD ==")
    check("C1 build_umo 形状", conversation_service.build_umo("abc") == "webchat:FriendMessage:webchat!astrbot_plugin_webui!abc")
    cid = uuid.uuid4().hex
    umo = conversation_service.build_umo(cid)
    check("C2 parse_umo 往返", pipeline_bridge.parse_umo(umo) == cid)
    check("C3 外平台拒绝", pipeline_bridge.parse_umo("aiocqhttp:GroupMessage:12345") is None)
    check("C4 非 FriendMessage 拒绝", pipeline_bridge.parse_umo("webchat:GroupMessage:webchat!astrbot_plugin_webui!x") is None)
    check("C5 非插件 creator 前缀拒绝", pipeline_bridge.parse_umo("webchat:FriendMessage:webchat!otheruser!x") is None)
    check("C6 残缺 umo 拒绝", pipeline_bridge.parse_umo("webchat") is None and pipeline_bridge.parse_umo("") is None)

    # 用户识别联动（v0.3.6 apply_sender_identity）：仅 identifier 开启时改写插件命名空间 sender
    ident_plugin = make_plugin(fresh_sandbox())

    def _ident_sender():
        return SimpleNamespace(user_id="astrbot_plugin_webui", nickname="astrbot_plugin_webui")

    def _ident_event(sender, umo_arg=None):
        return SimpleNamespace(
            unified_msg_origin=umo_arg if umo_arg is not None else umo,
            message_obj=SimpleNamespace(sender=sender),
        )

    ident_plugin.context.get_config = lambda: {"provider_settings": {"identifier": False}}
    sender = _ident_sender()
    pipeline_bridge.apply_sender_identity(ident_plugin, _ident_event(sender))
    check("C6a identifier 关不改写", sender.user_id == "astrbot_plugin_webui" and sender.nickname == "astrbot_plugin_webui", f"{sender.user_id}/{sender.nickname}")
    ident_plugin.context.get_config = lambda: {"provider_settings": {"identifier": True}}
    pipeline_bridge.apply_sender_identity(ident_plugin, _ident_event(sender))
    check("C6b identifier 开改写为配置名（schema 默认 USER）", sender.user_id == "USER" and sender.nickname == "USER", f"{sender.user_id}/{sender.nickname}")
    ident_plugin.config["user_display_name"] = "  泡泡  "
    sender = _ident_sender()
    pipeline_bridge.apply_sender_identity(ident_plugin, _ident_event(sender))
    check("C6c 配置名去首尾空白", sender.user_id == "泡泡" and sender.nickname == "泡泡", f"{sender.user_id}/{sender.nickname}")
    sender = _ident_sender()
    pipeline_bridge.apply_sender_identity(ident_plugin, _ident_event(sender, umo_arg="webchat:FriendMessage:webchat!otheruser!x"))
    check("C6d 官方会话页命名空间不改写", sender.user_id == "astrbot_plugin_webui" and sender.nickname == "astrbot_plugin_webui", f"{sender.user_id}/{sender.nickname}")
    ident_plugin.config["user_display_name"] = "   "
    sender = _ident_sender()
    pipeline_bridge.apply_sender_identity(ident_plugin, _ident_event(sender))
    check("C6e 空白名不改写", sender.user_id == "astrbot_plugin_webui" and sender.nickname == "astrbot_plugin_webui", f"{sender.user_id}/{sender.nickname}")
    ident_plugin.config["user_display_name"] = "USER"
    bare_plugin = make_plugin(fresh_sandbox())  # FakeContext 默认无 get_config → 桩降级
    sender = _ident_sender()
    pipeline_bridge.apply_sender_identity(bare_plugin, _ident_event(sender))
    check("C6f 桩环境无 get_config 降级不改写", sender.user_id == "astrbot_plugin_webui" and sender.nickname == "astrbot_plugin_webui", f"{sender.user_id}/{sender.nickname}")

    sandbox = fresh_sandbox()
    plugin = make_plugin(sandbox)
    context = plugin.context
    cm = context.conversation_manager
    db = context.db

    info = await conversation_service.create_conversation(context, "p1", None)
    check("C7 新建返回 uuid4hex cid", len(info["cid"]) == 32 and all(ch in "0123456789abcdef" for ch in info["cid"]))
    check("C8 缺省标题 新会话", info["title"] == "新会话" and cm.session_conversations[umo_of(info["cid"])].endswith(info["conversation_id"]))
    check("C9 platform_session 落库（creator=插件名/platform=webchat）", info["cid"] in db.platform_sessions and db.platform_sessions[info["cid"]].creator == "astrbot_plugin_webui" and db.platform_sessions[info["cid"]].platform_id == "webchat")
    check("C10 persona 绑定", cm.convs[info["conversation_id"]].persona_id == "p1")
    check("C11 双主键（cid != conversation_id）", info["cid"] != info["conversation_id"])
    check("C12 new_conversation 自动设当前会话", cm.session_conversations.get(umo_of(info["cid"])) == info["conversation_id"])
    check("C12b new_conversation 的 umo 与注入 umo 逐字符一致", cm.new_calls == [pipeline_bridge.build_umo(info["cid"])], str(cm.new_calls))

    # 改题：conv_id 必须经 get_curr_conversation_id 解析（禁传 cid）
    await conversation_service.rename_conversation(context, info["cid"], "这是一条超过二十个字的长标题应该被截断到二十字整")
    conv = await conversation_service.get_conversation(context, info["cid"])
    check("C13 改题截 20 字（conv_id 解析路径）", conv is not None and conv.title == "这是一条超过二十个字的长标题应该被截断到二十字整"[:20])
    check("C13b get_conversation 用 conv_id（非 cid）", bool(cm.get_calls) and cm.get_calls[-1][0] == pipeline_bridge.build_umo(info["cid"]) and cm.get_calls[-1][1] == info["conversation_id"] and cm.get_calls[-1][1] != info["cid"], str(cm.get_calls))
    check("C13c update_conversation 的 umo 与 new 一致", bool(cm.update_calls) and cm.update_calls[0][0] == cm.new_calls[0] and cm.update_calls[0][1] == info["conversation_id"], str(cm.update_calls))
    check("C14 update_conversation_title 未复活", "update_conversation_title(" not in inspect.getsource(conversation_service))

    # 列表：platform_sessions + updated_at 降序
    older = await conversation_service.create_conversation(context, None, None)
    db.platform_sessions[older["cid"]].updated_at = datetime.now(timezone.utc) - timedelta(hours=1)
    items = await conversation_service.list_conversations(context, sandbox)
    cids = [item["cid"] for item in items]
    check("C15 updated_at 降序", cids.index(info["cid"]) < cids.index(older["cid"]), str(cids))
    shape = items[0]
    check("C16 列表项契约键", set(shape.keys()) == {"cid", "title", "persona_id", "updated_at"}, str(shape.keys()))
    check("C17 updated_at ISO 带时区", "T" in shape["updated_at"])

    # 历史读取：平台历史映射（user + bot 含 reasoning/tools/usage/attachments）
    cid1 = info["cid"]
    hm = context.message_history_manager
    await hm.insert(
        platform_id="webchat", user_id=cid1,
        content={"type": "user", "message": [
            {"type": "plain", "text": "看看图"},
            {"type": "image", "attachment_id": "att_000001", "filename": "a.png"},
        ]},
        sender_id="astrbot_plugin_webui", sender_name="astrbot_plugin_webui", llm_checkpoint_id="cp1",
    )
    # 先造索引（附件反查依赖）
    disk = sandbox / "fake-att.png"
    disk.write_bytes(b"png")
    async with attachment_service._INDEX_LOCK:
        index = await attachment_service._load_index(sandbox)
        index["files"]["f_img1"] = {
            "file_id": "f_img1", "attachment_id": "att_000001", "kind": "image", "name": "a.png",
            "size": 3, "mime": "image/png", "preview": None, "path": disk.resolve().as_posix(),
            "cid": cid1, "created_at": uistore.now_iso(),
        }
        await attachment_service._save_index(sandbox, index)
    await hm.insert(
        platform_id="webchat", user_id=cid1,
        content={
            "type": "bot",
            "message": [
                {"type": "plain", "text": "这是一张图"},
                {"type": "tool_call", "tool_calls": [{"id": "call_1", "name": "get_weather", "args": {"city": "北京"}, "result": "晴", "finished_ts": 1.0}]},
                {"type": "think", "think": "想了想"},
            ],
            "reasoning": "想了想",
            "agent_stats": {"token_usage": {"input_other": 3, "input_cached": 2, "output": 5}},
        },
        sender_id="bot", sender_name="bot", llm_checkpoint_id="cp1",
    )
    result = await conversation_service.page_history(context, sandbox, cid1, 1, 30)
    check("C18 平台历史分页契约键", set(result.keys()) == {"cid", "page", "size", "total", "has_more", "messages"}, str(result.keys()))
    check("C19 历史两条（user+bot）", [m["role"] for m in result["messages"]] == ["user", "assistant"], str(result["messages"]))
    bot_msg = result["messages"][1]
    check("C20 bot 文本/reasoning/usage", bot_msg["text"] == "这是一张图" and bot_msg["reasoning"] == {"content": "想了想"} and bot_msg["usage"] == {"output": 5, "total": 10}, str(bot_msg))
    check("C21 bot tools 映射", bot_msg["tools"][0]["name"] == "get_weather" and bot_msg["tools"][0]["result"] == "晴", str(bot_msg.get("tools")))
    user_msg = result["messages"][0]
    check("C22 user 附件索引反查", [a["file_id"] for a in user_msg.get("attachments") or []] == ["f_img1"], str(user_msg.get("attachments")))
    check("C23 消息 id 平台行号同源", bot_msg["id"].startswith("m_") and user_msg["id"].startswith("m_"))

    # 分页语义：page 1 最新、页内旧->新、has_more 配 count（补 3 行凑 5 行）
    for i in range(3):
        await hm.insert(
            platform_id="webchat", user_id=cid1,
            content={"type": "user", "message": [{"type": "plain", "text": f"u{i + 3}"}]},
            sender_id="astrbot_plugin_webui", sender_name="astrbot_plugin_webui", llm_checkpoint_id="cp1",
        )
    p1 = await conversation_service.page_history(context, sandbox, cid1, 1, 2)
    check("C-P1 page=1 最新两条（页内旧->新）", [m["text"] for m in p1["messages"]] == ["u4", "u5"], str([m["text"] for m in p1["messages"]]))
    check("C-P2 has_more 用 count", p1["total"] == 5 and p1["has_more"] is True)
    p2 = await conversation_service.page_history(context, sandbox, cid1, 2, 2)
    check("C-P3 page=2 中段（bot 在前）", [m["text"] for m in p2["messages"]] == ["这是一张图", "u3"], str([m["text"] for m in p2["messages"]]))
    p3 = await conversation_service.page_history(context, sandbox, cid1, 3, 2)
    check("C-P4 末页最早一条 + has_more=False", [m["text"] for m in p3["messages"]] == ["看看图"] and p3["has_more"] is False)

    # legacy 分支：无 platform_session + 有归档 -> legacy:true
    legacy_cid = uuid.uuid4().hex
    _write_archive(sandbox, legacy_cid, [{"id": "m_l", "role": "user", "text": "旧消息"}])
    legacy_result = await conversation_service.page_history(context, sandbox, legacy_cid, 1, 30)
    check("C24 legacy 响应附 legacy:true", legacy_result.get("legacy") is True and legacy_result["messages"][0].get("legacy") is True, str(legacy_result))
    try:
        await conversation_service.page_history(context, sandbox, uuid.uuid4().hex, 1, 30)
        check("C25 无宿主无归档 404", False)
    except conversation_service.ConversationNotFoundError:
        check("C25 无宿主无归档 404", True)

    # 删除：宿主级联（platform_session 存在）
    cid2 = older["cid"]
    await conversation_service.delete_conversation(context, cid2, sandbox)
    check("C26 级联 delete_conversations_by_user_id", umo_of(cid2) in cm.deleted_by_user)
    check("C27 级联 delete_platform_session", cid2 in db.deleted_sessions)
    check("C28 级联平台历史删（offset_sec=99999999）", any(d["offset_sec"] == 99999999 and d["user_id"] == cid2 for d in hm.deleted_offsets), str(hm.deleted_offsets))

    # 删除：legacy 分支（无 platform_session）只删归档文件，不触宿主
    host_deletes_before = len(cm.deleted_by_user)
    await conversation_service.delete_conversation(context, legacy_cid, sandbox)
    check("C29 legacy 删归档文件", not (sandbox / "ui_history" / f"{legacy_cid}.json").exists())
    check("C30 legacy 不触宿主", len(cm.deleted_by_user) == host_deletes_before and legacy_cid not in db.deleted_sessions)


def umo_of(cid: str) -> str:
    """测试辅助：cid -> 插件命名空间 umo。"""
    return pipeline_bridge.build_umo(cid)


# ---------------------------------------------------------------------------
# D. chat_service.py 生成内核（v0.2）：注入-排水 / 翻译表 / stop / 超时 / 队列纪律
# （脚本化载荷序列驱动桩 back_queue；宿主管道行为以 webchat_event.py 实源为准）
# ---------------------------------------------------------------------------


def _std_frames(gen_id: str) -> list[dict]:
    """标准脚本化载荷：翻译表逐型 + 坏载荷防御 + 冗余帧（D1/E36 复用）。

    期望 SSE 事件序列：reasoning_delta / text_delta / tool_call / tool_result /
    text_delta / done（坏 JSON tool_call、非 dict 载荷、message_id 不匹配、
    run_started、complete 均不产事件）。
    """
    return [
        {"type": "run_started", "data": {"run_id": gen_id}, "streaming": False, "message_id": gen_id},
        {"type": "plain", "data": "思考中", "streaming": True, "chain_type": "reasoning", "message_id": gen_id},
        {"type": "plain", "data": "今天", "streaming": True, "chain_type": None, "message_id": gen_id},
        # 坏载荷防御：坏 JSON tool_call / 非 dict 载荷 / message_id 不匹配 —— 不炸流、不产无意义事件
        {"type": "plain", "data": "{not-json", "streaming": True, "chain_type": "tool_call", "message_id": gen_id},
        "garbage-payload",
        {"type": "plain", "data": "忽略", "streaming": True, "chain_type": None, "message_id": "other-gen"},
        {"type": "plain", "data": json.dumps({"id": "c1", "name": "get_weather", "args": {"city": "北京"}, "ts": 1.0}), "streaming": True, "chain_type": "tool_call", "message_id": gen_id},
        {"type": "plain", "data": json.dumps({"id": "c1", "ts": 2.0, "result": "晴 26℃"}), "streaming": True, "chain_type": "tool_call_result", "message_id": gen_id},
        {"type": "plain", "data": "晴", "streaming": True, "chain_type": None, "message_id": gen_id},
        {"type": "plain", "data": json.dumps({"token_usage": {"input_other": 3, "input_cached": 2, "output": 5}, "current_context_tokens": 10}), "streaming": True, "chain_type": "agent_stats", "message_id": gen_id},
        {"type": "complete", "data": "今天晴", "reasoning": "思考中", "streaming": True, "message_id": gen_id},  # 冗余帧：正文/思考已由流式 plain 覆盖
        {"type": "end", "data": "", "streaming": False, "message_id": gen_id},
    ]


async def _test_chat_happy_path() -> None:
    print("== D1. 注入-排水全链：翻译表逐型 + bot 落库 + done + 队列纪律 ==")
    chat_service._GENERATIONS.clear()
    _stub_registry().stop_requests.clear()
    sandbox = fresh_sandbox()
    text = "第一句话测试自动改题"
    plugin = make_plugin(sandbox)
    info = await conversation_service.create_conversation(plugin.context, None, None)
    cid = info["cid"]
    mgr = _stub_queue_mgr()
    hm = plugin.context.message_history_manager

    gen_id = await chat_service.start_generation(plugin, cid, text)
    check("D1-1 gen_id uuid4hex", len(gen_id) == 32)
    check("D1-2 进行中可查", chat_service.active_generation_id(cid) == gen_id)
    # 生成任务是 create_task 调度的：等注入三步落地（平台历史 + back_queue）再断言
    check("D1-3 注入落地（平台历史+back_queue）", await wait_for(lambda: gen_id in mgr.back_queues and bool(hm.rows), timeout=2))

    # 注入三步形状（§0.2：先落平台历史 -> back_queue -> 会话队列 put）
    check("D1-4 用户消息先落平台历史", hm.rows and hm.rows[0].content["type"] == "user")
    check("D1-5 落库 parts 剥 path 且含 plain", hm.rows[0].content["message"] == [{"type": "plain", "text": text}])
    check("D1-6 sender 自补 creator", hm.rows[0].sender_id == "astrbot_plugin_webui" and hm.rows[0].sender_name == "astrbot_plugin_webui")
    item = await asyncio.wait_for(mgr.queues[cid].get(), timeout=2)
    creator, qcid, payload = item
    check("D1-7 队列 tuple (creator, cid, payload)", creator == "astrbot_plugin_webui" and qcid == cid)
    check("D1-8 payload message_id=gen_id + 流式 flags", payload["message_id"] == gen_id and payload["flags"] == {"enable_streaming": True, "enable_reasoning": True})
    check("D1-9 checkpoint 注入/落库同源", payload["llm_checkpoint_id"] == hm.inserts[0]["checkpoint"])

    await feed_frames(gen_id, _std_frames(gen_id))
    entry = chat_service._GENERATIONS[gen_id]
    await asyncio.wait_for(entry["task"], timeout=10)

    events = [e["event"] for e in entry["buffer"]]
    check("D1-10 翻译表逐型", events == ["reasoning_delta", "text_delta", "tool_call", "tool_result", "text_delta", "stats", "done"], str(events))
    check("D1-11 seq 从 0 单调", [e["data"]["seq"] for e in entry["buffer"]] == list(range(7)))
    check("D1-12 delta 内容", (entry["buffer"][0]["data"]["delta"], entry["buffer"][1]["data"]["delta"], entry["buffer"][4]["data"]["delta"]) == ("思考中", "今天", "晴"))
    check("D1-13 坏载荷/不匹配/冗余帧不产事件", len(entry["buffer"]) == 7)
    tool_call = entry["buffer"][2]["data"]
    check("D1-14 tool_call 契约", tool_call == {"seq": tool_call["seq"], "phase": "start", "id": "c1", "name": "get_weather", "args": {"city": "北京"}}, str(tool_call))
    tool_result = entry["buffer"][3]["data"]
    check(
        "D1-15 tool_result result|error 互斥 + elapsed_ms>=0",
        tool_result.get("id") == "c1" and tool_result.get("result") == "晴 26℃"
        and "error" not in tool_result and isinstance(tool_result.get("elapsed_ms"), int) and tool_result["elapsed_ms"] >= 0,
        str(tool_result),
    )
    stats_evt = entry["buffer"][5]["data"]
    check(
        "D1-32 stats 事件字段（cache_rate/context_tokens/max=None）",
        stats_evt == {"seq": stats_evt["seq"], "cache_rate": 0.4, "context_tokens": 10, "max_context_tokens": None, "input_cached": 2, "input_other": 3, "output": 5},
        str(stats_evt),
    )
    done = entry["buffer"][6]["data"]
    bot_rows = [r for r in hm.rows if r.content.get("type") == "bot"]
    check("D1-16 done.usage 来自 agent_stats", done["usage"] == {"output": 5, "total": 10}, str(done))
    check("D1-17 done.message_id=平台行号同源", len(bot_rows) == 1 and done["message_id"] == f"m_{bot_rows[0].id}")
    content = bot_rows[0].content
    check("D1-18 bot content 形状（type/reasoning/agent_stats）", content["type"] == "bot" and content.get("reasoning") == "思考中" and content.get("agent_stats", {}).get("token_usage", {}).get("output") == 5, str(content)[:200])
    part_types = [p["type"] for p in content["message"]]
    # 宿主 BotMessageAccumulator 语义：工具段前 flush 正文缓冲，"晴" 另起 plain（对照 :244-267 实源）
    check("D1-19 bot parts 形状 think/plain/tool_call/plain", part_types == ["think", "plain", "tool_call", "plain"], str(part_types))
    plain_parts = [p for p in content["message"] if p["type"] == "plain"]
    check("D1-20 bot 正文分缓冲累积", [p["text"] for p in plain_parts] == ["今天", "晴"], str(plain_parts))
    tool_part = [p for p in content["message"] if p["type"] == "tool_call"][0]["tool_calls"][0]
    check("D1-21 bot 工具段回填", tool_part["id"] == "c1" and tool_part["result"] == "晴 26℃", str(tool_part))
    check("D1-22 bot sender=bot + checkpoint 同源", bot_rows[0].sender_id == "bot" and hm.inserts[1]["checkpoint"] == payload["llm_checkpoint_id"])
    # 队列纪律 + 任务收尾
    check("D1-23 正常路径队列已清理", gen_id not in mgr.back_queues and gen_id not in mgr._request_conversation)
    check("D1-24 任务正常收尾（非取消）", entry["task"].done() and not entry["task"].cancelled())
    check("D1-25 终态后 active=None", chat_service.active_generation_id(cid) is None)
    check("D1-26 存活期内 generation_exists", chat_service.generation_exists(gen_id) is True)
    check("D1-27 重放幂等", [e["event"] for e in entry["buffer"]] == [e["event"] for e in await chat_service.replay_events(gen_id)])
    # 双主键：自动改题经 conv_id；new_conversation/update umo 逐字符一致
    cm = plugin.context.conversation_manager
    check("D1-28 自动改题经 conv_id（非 cid）", bool(cm.title_updates) and cm.title_updates[0][0] == info["conversation_id"] and cm.title_updates[0][0] != cid, str(cm.title_updates))
    check("D1-29 new_conversation/update umo 逐字符一致", cm.update_calls and cm.update_calls[0][0] == cm.new_calls[0] == pipeline_bridge.build_umo(cid), str(cm.update_calls))
    check("D1-30 改题截 20 字", cm.update_calls[0][2] == text[:20])
    check("D1-31 未知 gen_id stop 404", await chat_service.abort_generation(uuid.uuid4().hex) is False)


async def _test_chat_session_busy() -> None:
    print("== D2. 同会话 single-flight 409 ==")
    chat_service._GENERATIONS.clear()
    sandbox = fresh_sandbox()
    plugin = make_plugin(sandbox)
    cid = (await conversation_service.create_conversation(plugin.context, None, None))["cid"]

    gen_id = await chat_service.start_generation(plugin, cid, "第一条")
    check("D2-1 首任务占位（无帧保持进行中）", chat_service.active_generation_id(cid) == gen_id)
    try:
        await chat_service.start_generation(plugin, cid, "第二条")
        check("D2-2 同会话 409 session_busy", False)
    except chat_service.SessionBusyError as exc:
        check("D2-2 同会话 409 session_busy", exc.status_code == 409 and exc.slug == "session_busy")
    check("D2-3 被拒者未落用户消息", await wait_for(lambda: len([r for r in plugin.context.message_history_manager.rows if r.content.get("type") == "user"]) == 1, timeout=2))

    await feed_frames(gen_id, [{"type": "end", "data": "", "streaming": False, "message_id": gen_id}])
    await asyncio.wait_for(chat_service._GENERATIONS[gen_id]["task"], timeout=5)
    check("D2-4 收尾后 active=None", chat_service.active_generation_id(cid) is None)
    check("D2-5 空回复不落 bot 行", not [r for r in plugin.context.message_history_manager.rows if r.content.get("type") == "bot"])


async def _test_chat_concurrency() -> None:
    print("== D3. 全局并发上限实时读取配置 ==")
    chat_service._GENERATIONS.clear()
    sandbox = fresh_sandbox()
    plugin = make_plugin(sandbox, config={"max_concurrent_generations": 1})
    c1 = (await conversation_service.create_conversation(plugin.context, None, None))["cid"]
    c2 = (await conversation_service.create_conversation(plugin.context, None, None))["cid"]

    g1 = await chat_service.start_generation(plugin, c1, "一")
    try:
        await chat_service.start_generation(plugin, c2, "二")
        check("D3-1 并发满 409 concurrency_limit", False)
    except chat_service.ConcurrencyLimitError as exc:
        check("D3-1 并发满 409 concurrency_limit", exc.status_code == 409 and exc.slug == "concurrency_limit")

    plugin.config["max_concurrent_generations"] = 2  # 热生效：实时读 self.config
    g2 = await chat_service.start_generation(plugin, c2, "二")
    check("D3-2 配置实时读取后放行", chat_service.active_generation_id(c2) == g2)

    await feed_frames(g1, [{"type": "end", "data": "", "streaming": False, "message_id": g1}])
    await feed_frames(g2, [{"type": "end", "data": "", "streaming": False, "message_id": g2}])
    await asyncio.wait_for(
        asyncio.gather(chat_service._GENERATIONS[g1]["task"], chat_service._GENERATIONS[g2]["task"]),
        timeout=5,
    )
    check("D3-3 双任务收尾归零", chat_service._active_count() == 0)


async def _test_chat_abort() -> None:
    print("== D4. stop 先落终态 + 优雅中止 + 消费任务不被 cancel + 部分回复落历史 ==")
    chat_service._GENERATIONS.clear()
    registry = _stub_registry()
    registry.stop_requests.clear()
    sandbox = fresh_sandbox()
    plugin = make_plugin(sandbox)
    cid = (await conversation_service.create_conversation(plugin.context, None, None))["cid"]

    gen_id = await chat_service.start_generation(plugin, cid, "测试中止的部分回复")
    entry = chat_service._GENERATIONS[gen_id]
    await feed_frames(gen_id, [{"type": "plain", "data": "部分", "streaming": True, "chain_type": None, "message_id": gen_id}])
    check("D4-1 首个增量已缓冲", await wait_for(lambda: any(e["event"] == "text_delta" for e in entry["buffer"])))

    check("D4-2 stop 返回 True", await chat_service.abort_generation(gen_id) is True)
    check("D4-3 stopped 终态已入缓冲", await wait_for(lambda: any(e["event"] == "stopped" for e in entry["buffer"])))
    check("D4-4 事件序列 text_delta+stopped", [e["event"] for e in entry["buffer"]] == ["text_delta", "stopped"])
    stopped = entry["buffer"][-1]["data"]
    check("D4-5 stopped 文案契约", stopped["message"] == "已手动停止", str(stopped))
    check("D4-6 registry.request_agent_stop_all(umo) 被调用", registry.stop_requests == [pipeline_bridge.build_umo(cid)], str(registry.stop_requests))
    check("D4-7 消费任务未被 cancel（继续排水等 end）", not entry["task"].done())
    check("D4-8 已终态再 stop -> False(404)", await chat_service.abort_generation(gen_id) is False)

    received = [item async for item in chat_service.iter_events(gen_id)]
    check("D4-9 重订阅全量重放且终态关流", [i["event"] for i in received] == ["text_delta", "stopped"])

    await feed_frames(gen_id, [{"type": "end", "data": "", "streaming": False, "message_id": gen_id}])
    await asyncio.wait_for(entry["task"], timeout=5)
    check("D4-10 排水到 end 自然收尾（非取消）", entry["task"].done() and not entry["task"].cancelled())
    bot_rows = [r for r in plugin.context.message_history_manager.rows if r.content.get("type") == "bot"]
    check(
        "D4-11 部分回复照常落平台历史",
        len(bot_rows) == 1 and pipeline_bridge.collect_plain_text_from_message_parts(bot_rows[0].content["message"]) == "部分",
        str(bot_rows[0].content if bot_rows else None),
    )
    check("D4-12 队列已清理", gen_id not in _stub_queue_mgr().back_queues)
    check("D4-13 终态后 active=None", chat_service.active_generation_id(cid) is None)


async def _test_chat_idle_timeout() -> None:
    print("== D5. 排水空闲超时兜底（注入短超时触发）==")
    chat_service._GENERATIONS.clear()
    sandbox = fresh_sandbox()
    plugin = make_plugin(sandbox)
    cid = (await conversation_service.create_conversation(plugin.context, None, None))["cid"]
    real_timeout = pipeline_bridge.IDLE_TIMEOUT_SECONDS
    try:
        pipeline_bridge.IDLE_TIMEOUT_SECONDS = 0.25  # consume_generation 调用时解析常量，可注入
        gen_id = await chat_service.start_generation(plugin, cid, "永不回帧")
        entry = chat_service._GENERATIONS[gen_id]
        await asyncio.wait_for(entry["task"], timeout=10)
        events = [e["event"] for e in entry["buffer"]]
        check("D5-1 空闲超时写 error 终态", events == ["error"], str(events))
        err = entry["buffer"][-1]["data"]
        check("D5-2 error fatal+code+文案", err.get("fatal") is True and err.get("code") == "server_error" and "超时" in err.get("message", ""), str(err))
        check("D5-3 超时路径队列已清理", gen_id not in _stub_queue_mgr().back_queues)
        check("D5-4 终态后 active=None", chat_service.active_generation_id(cid) is None)
    finally:
        pipeline_bridge.IDLE_TIMEOUT_SECONDS = real_timeout


async def _test_chat_queue_discipline() -> None:
    print("== D6. remove_back_queue 纪律（异常/取消/shutdown 三路径）==")
    chat_service._GENERATIONS.clear()
    mgr = _stub_queue_mgr()
    sandbox = fresh_sandbox()
    plugin = make_plugin(sandbox)

    # 异常路径：emit 抛错 -> 排水循环异常逃逸 -> finally 清队列（不依赖外层兜底）
    gen_x = f"genx{uuid.uuid4().hex}"
    bq = mgr.get_or_create_back_queue(gen_x, "cidx")
    await bq.put({"type": "plain", "data": "x", "streaming": True, "chain_type": None, "message_id": gen_x})

    def _boom(event, data):  # emit 契约是同步回调（chat_service._make_emit 同款）
        raise RuntimeError("emit boom")

    try:
        await asyncio.wait_for(
            pipeline_bridge.consume_generation(plugin.context, "cidx", gen_x, bq, "cp", _boom, idle_timeout=2.0),
            timeout=5,
        )
        check("D6-1 emit 抛错异常逃逸", False)
    except RuntimeError:
        check("D6-1 emit 抛错异常逃逸", True)
    check("D6-2 异常路径队列已清理", gen_x not in mgr.back_queues)

    # 取消路径：cancel_generation_tasks（级联删除专用）先落 stopped 再 cancel
    info = await conversation_service.create_conversation(plugin.context, None, None)
    cid = info["cid"]
    gen_id = await chat_service.start_generation(plugin, cid, "会被取消")
    await chat_service.cancel_generation_tasks(cid, wait=True)
    entry = chat_service._GENERATIONS[gen_id]
    check("D6-3 取消路径任务已收尾（cancelled）", entry["task"].done() and entry["task"].cancelled())
    check("D6-4 取消前落 stopped 终态", [e["event"] for e in entry["buffer"]] == ["stopped"])
    check("D6-5 取消路径队列已清理", gen_id not in mgr.back_queues)

    # shutdown 路径：terminate 全量落终态 + cancel + 清队列
    gen2 = await chat_service.start_generation(plugin, cid, "shutdown 清理")
    await chat_service.shutdown()
    entry2 = chat_service._GENERATIONS[gen2]
    check("D6-6 shutdown 落 stopped 终态", [e["event"] for e in entry2["buffer"]] == ["stopped"])
    check("D6-7 shutdown 队列已清理", gen2 not in mgr.back_queues and gen_id not in mgr.back_queues)
    check("D6-8 shutdown 任务已收尾", entry2["task"].done() and entry2["task"].cancelled())


async def _test_chat_media_frames() -> None:
    print("== D7. bot 媒体帧：db 注册 + 索引 + media 事件上屏 + 助手 attachments；缺文件防御 ==")
    chat_service._GENERATIONS.clear()
    sandbox = fresh_sandbox()
    hostdir = set_stub_host_data(sandbox) / "attachments"
    hostdir.mkdir(parents=True, exist_ok=True)
    plugin = make_plugin(sandbox)
    cid = (await conversation_service.create_conversation(plugin.context, None, None))["cid"]
    (hostdir / "bot_pic.png").write_bytes(b"pngdata")
    (hostdir / "store.bin").write_bytes(b"bindata")
    (hostdir / "bot_voice.wav").write_bytes(b"wavdata")
    db_count_before = len(plugin.context.db.attachments)

    # D7-8/D7-11 的「preview 键省略」断言与运行环境有无 Pillow 解耦：替身恒返
    # None（真 _image_preview 在装有 PIL 的机器上可能产出 preview data URL，
    # public_meta 会带上该键，断言结果将随环境漂移）；finally 还原
    original_image_preview = attachment_service._image_preview
    attachment_service._image_preview = lambda path: None
    try:
        gen_id = await chat_service.start_generation(plugin, cid, "发图给我")
        frames = [
            {"type": "image", "data": "[IMAGE]bot_pic.png", "streaming": True, "message_id": gen_id},
            {"type": "file", "data": "[FILE]store.bin|原始名.bin", "streaming": True, "message_id": gen_id},
            {"type": "record", "data": "[RECORD]bot_voice.wav", "streaming": True, "message_id": gen_id},
            {"type": "image", "data": "[IMAGE]missing.png", "streaming": True, "message_id": gen_id},  # 缺文件防御
            {"type": "end", "data": "", "streaming": False, "message_id": gen_id},
        ]
        await feed_frames(gen_id, frames)
        entry = chat_service._GENERATIONS[gen_id]
        await asyncio.wait_for(entry["task"], timeout=10)
    finally:
        attachment_service._image_preview = original_image_preview
    db = plugin.context.db

    check("D7-1 媒体帧注册 db（缺文件跳过）", len(db.attachments) == db_count_before + 3, str(db.attachments))
    atts = list(db.attachments.values())[-3:]
    check("D7-2 db type/落盘路径", {a.type for a in atts} == {"image", "file", "record"} and all(a.path.startswith(str(hostdir)) for a in atts), str(atts))
    bot_rows = [r for r in plugin.context.message_history_manager.rows if r.content.get("type") == "bot"]
    media_parts = [p for p in bot_rows[0].content["message"] if p["type"] in ("image", "file", "record")] if bot_rows else []
    check("D7-3 bot parts 含媒体段（attachment_id）", len(media_parts) == 3 and all(p.get("attachment_id") for p in media_parts), str(media_parts))
    file_part = next((p for p in media_parts if p["type"] == "file"), {})
    check("D7-4 file 展示名/stored_filename", file_part.get("filename") == "原始名.bin" and file_part.get("stored_filename") == "store.bin", str(file_part))
    result = await conversation_service.page_history(plugin.context, sandbox, cid, 1, 30)
    bot_msg = result["messages"][-1]
    check(
        "D7-5 助手 attachments 索引反查（record kind 归一 file）",
        [a["kind"] for a in bot_msg.get("attachments") or []] == ["image", "file", "file"]
        and bot_msg["attachments"][0]["name"] == "bot_pic.png"
        and bot_msg["attachments"][1]["name"] == "原始名.bin"
        and bot_msg["attachments"][2]["name"] == "bot_voice.wav",
        str(bot_msg.get("attachments")),
    )
    # media 事件：注册成功逐帧 emit（缺文件帧不发），载荷 = {"attachment": 契约 meta}
    media_events = [e for e in entry["buffer"] if e["event"] == "media"]
    check("D7-6 media 事件逐帧上屏（仅 done 终态）", [e["event"] for e in entry["buffer"]] == ["media", "media", "media", "done"], str([e["event"] for e in entry["buffer"]]))
    check("D7-6b media 载荷形状 {seq, attachment}", all(set(e["data"].keys()) == {"seq", "attachment"} for e in media_events), str(media_events))
    pub = [e["data"]["attachment"] for e in media_events]
    check("D7-8 image 帧 kind/name + preview 缺省省略", pub[0]["kind"] == "image" and pub[0]["name"] == "bot_pic.png" and "preview" not in pub[0], str(pub[0]))
    check("D7-9 file 帧 kind/name（display 优先）", pub[1]["kind"] == "file" and pub[1]["name"] == "原始名.bin", str(pub[1]))
    check("D7-10 record 帧 kind 归入 file/name 存储名", pub[2]["kind"] == "file" and pub[2]["name"] == "bot_voice.wav", str(pub[2]))
    check(
        "D7-11 契约键集合 {file_id,kind,name,size,mime}",
        all(set(a.keys()) == {"file_id", "kind", "name", "size", "mime"} for a in pub)
        and all(a["file_id"].startswith("f_") and a["size"] > 0 for a in pub),
        str(pub),
    )
    check("D7-12 media 与历史 attachments 同名同源", [a["name"] for a in pub] == [a["name"] for a in bot_msg.get("attachments") or []], str(pub))
    check("D7-13 队列已清理", gen_id not in _stub_queue_mgr().back_queues)


async def _test_streaming_flags() -> None:
    print("== D10. 流式配置适配（注入 flags 回读宿主配置 + config 端点透传）==")
    chat_service._GENERATIONS.clear()
    mgr = _stub_queue_mgr()
    sandbox = fresh_sandbox()
    plugin = make_plugin(sandbox)
    cid = (await conversation_service.create_conversation(plugin.context, None, None))["cid"]

    # streaming_response=False：注入 flags.enable_streaming 必须为 False（宿主覆盖语义）
    plugin.context.get_config = lambda: {"provider_settings": {"streaming_response": False}}
    gen1 = uuid.uuid4().hex
    inj = await pipeline_bridge.inject_user_message(
        plugin.context, cid, [{"type": "plain", "text": "非流式"}], gen1
    )
    payload1 = await asyncio.wait_for(mgr.queues[cid].get(), timeout=2)
    check("D10-1 关流式 flags.enable_streaming=False", payload1[2]["flags"] == {"enable_streaming": False, "enable_reasoning": True}, str(payload1[2]["flags"]))
    check("D10-2 注入返回 back_queue/checkpoint", inj["back_queue"] is mgr.back_queues[gen1] and isinstance(inj["llm_checkpoint_id"], str) and inj["llm_checkpoint_id"] == payload1[2]["llm_checkpoint_id"])

    # streaming_response=True / 键缺失 / 桩无 get_config：True
    plugin.context.get_config = lambda: {"provider_settings": {"streaming_response": True}}
    gen2 = uuid.uuid4().hex
    await pipeline_bridge.inject_user_message(plugin.context, cid, [{"type": "plain", "text": "流式"}], gen2)
    payload2 = await asyncio.wait_for(mgr.queues[cid].get(), timeout=2)
    check("D10-3 开流式 flags.enable_streaming=True", payload2[2]["flags"]["enable_streaming"] is True, str(payload2[2]["flags"]))
    plugin.context.get_config = lambda: {}
    gen3 = uuid.uuid4().hex
    await pipeline_bridge.inject_user_message(plugin.context, cid, [{"type": "plain", "text": "缺键"}], gen3)
    payload3 = await asyncio.wait_for(mgr.queues[cid].get(), timeout=2)
    check("D10-4 键缺失兜底 True", payload3[2]["flags"]["enable_streaming"] is True)
    del plugin.context.get_config  # 桩无 get_config（属性级兜底路径）
    gen4 = uuid.uuid4().hex
    await pipeline_bridge.inject_user_message(plugin.context, cid, [{"type": "plain", "text": "无配置"}], gen4)
    payload4 = await asyncio.wait_for(mgr.queues[cid].get(), timeout=2)
    check("D10-5 桩无 get_config 兜底 True", payload4[2]["flags"]["enable_streaming"] is True)

    # config 端点透传同源值（user_display_name 并存）
    plugin.context.get_config = lambda: {"provider_settings": {"streaming_response": False}}
    resp = await routes.handle_get_config(plugin)
    check("D10-6 config 端点 streaming_response=False", resp["status"] == 200 and resp["data"]["streaming_response"] is False and resp["data"]["user_display_name"] == "USER", str(resp))
    plugin.context.get_config = lambda: {"provider_settings": {}}
    resp = await routes.handle_get_config(plugin)
    check("D10-7 config 端点缺键兜底 True", resp["data"]["streaming_response"] is True)

    for gen in (gen1, gen2, gen3, gen4):
        pipeline_bridge.remove_back_queue(gen)
    mgr.remove_queues(cid)
    plugin.context.message_history_manager.rows.clear()


async def _test_chat_concurrent_send() -> None:
    print("== D8. 并发 send 竞态（TOCTOU 回归）==")
    chat_service._GENERATIONS.clear()
    sandbox = fresh_sandbox()
    plugin = make_plugin(sandbox)
    cid = (await conversation_service.create_conversation(plugin.context, None, None))["cid"]

    results = await asyncio.gather(
        chat_service.start_generation(plugin, cid, "并发甲"),
        chat_service.start_generation(plugin, cid, "并发乙"),
        return_exceptions=True,
    )
    ok = [r for r in results if isinstance(r, str)]
    busy = [r for r in results if isinstance(r, chat_service.SessionBusyError)]
    check("D8-1 同 cid 并发恰一条放行", len(ok) == 1 and len(busy) == 1, f"ok={ok} busy={len(busy)}")
    check("D8-2 放行者持 single-flight 锁", chat_service.active_generation_id(cid) == ok[0])
    check("D8-3 被拒者未落用户消息", len([r for r in plugin.context.message_history_manager.rows if r.content.get("type") == "user"]) == 1)
    await feed_frames(ok[0], [{"type": "end", "data": "", "streaming": False, "message_id": ok[0]}])
    await asyncio.wait_for(chat_service._GENERATIONS[ok[0]]["task"], timeout=5)

    chat_service._GENERATIONS.clear()  # 第一段已终态，清表无泄漏
    plugin2 = make_plugin(sandbox, config={"max_concurrent_generations": 1})
    c1 = (await conversation_service.create_conversation(plugin2.context, None, None))["cid"]
    c2 = (await conversation_service.create_conversation(plugin2.context, None, None))["cid"]
    results2 = await asyncio.gather(
        chat_service.start_generation(plugin2, c1, "并发一"),
        chat_service.start_generation(plugin2, c2, "并发二"),
        return_exceptions=True,
    )
    ok2 = [r for r in results2 if isinstance(r, str)]
    limit = [r for r in results2 if isinstance(r, chat_service.ConcurrencyLimitError)]
    check("D8-4 limit=1 并发恰一条放行", len(ok2) == 1 and len(limit) == 1, f"ok={len(ok2)} limit={len(limit)}")
    check("D8-5 活跃任务数不超过上限", chat_service._active_count() == 1)
    for gen_id in ok2:
        await feed_frames(gen_id, [{"type": "end", "data": "", "streaming": False, "message_id": gen_id}])
    await asyncio.wait_for(asyncio.gather(*(chat_service._GENERATIONS[g]["task"] for g in ok2)), timeout=5)
    check("D8-6 全部收尾后活跃归零", chat_service._active_count() == 0)


async def _test_delete_while_generating() -> None:
    print("== D9. 删除生成中的会话（取消消费任务先行，已删历史不复活）==")
    chat_service._GENERATIONS.clear()
    sandbox = fresh_sandbox()
    plugin = make_plugin(sandbox)
    cid = (await conversation_service.create_conversation(plugin.context, None, None))["cid"]
    gen_id = await chat_service.start_generation(plugin, cid, "会被中止的消息")
    entry = chat_service._GENERATIONS[gen_id]
    await feed_frames(gen_id, [{"type": "plain", "data": "部分", "streaming": True, "chain_type": None, "message_id": gen_id}])
    check("D9-0 挂起生成已有增量", await wait_for(lambda: len(entry["buffer"]) > 0))

    # routes.delete 流程：先取消消费任务（落终态、等收尾），再级联删除
    await chat_service.cancel_generation_tasks(cid, wait=True)
    check("D9-1 取消后任务收尾（cancelled）", entry["task"].done() and entry["task"].cancelled())
    check("D9-2 取消前落 stopped 终态", [e["event"] for e in entry["buffer"]] == ["text_delta", "stopped"])
    check("D9-3 取消路径队列已清理", gen_id not in _stub_queue_mgr().back_queues)
    check("D9-4 取消路径不落 bot 历史（半截内容不复活）", not [r for r in plugin.context.message_history_manager.rows if r.content.get("type") == "bot"])

    await conversation_service.delete_conversation(plugin.context, cid, sandbox)
    check("D9-5 platform_session 级联删除", cid in plugin.context.db.deleted_sessions)
    check("D9-6 平台历史级联清空", plugin.context.message_history_manager.rows == [])
    check("D9-7 历史 offset_sec=99999999", any(d["offset_sec"] == 99999999 and d["user_id"] == cid for d in plugin.context.message_history_manager.deleted_offsets))
    check("D9-8 宿主会话级联删除", pipeline_bridge.build_umo(cid) in plugin.context.conversation_manager.deleted_by_user)
    check("D9-9 remove_queues（会话队列+back 队列）", cid not in _stub_queue_mgr().queues and gen_id not in _stub_queue_mgr().back_queues)
    check("D9-10 删除后无活跃任务", chat_service.active_generation_id(cid) is None)


async def _test_sse_ping() -> None:
    print("== J6. SSE 保活 ping（空闲超时发帧，不占业务 seq）==")
    chat_service._GENERATIONS.clear()
    sandbox = fresh_sandbox()
    plugin = make_plugin(sandbox)
    cid = (await conversation_service.create_conversation(plugin.context, None, None))["cid"]
    gen_id = await chat_service.start_generation(plugin, cid, "挂着的生成")
    entry = chat_service._GENERATIONS[gen_id]
    await feed_frames(gen_id, [{"type": "plain", "data": "慢", "streaming": True, "chain_type": None, "message_id": gen_id}])
    check("J6-0 业务帧已缓冲", await wait_for(lambda: len(entry["buffer"]) > 0))

    real_interval = routes.SSE_PING_INTERVAL_SECONDS
    try:
        routes.SSE_PING_INTERVAL_SECONDS = 0.05  # 缩短空闲阈值，测试可观测
        stream = await routes.handle_stream(plugin, gen_id)
        check("J6-1 stream 200", stream["__type"] == "stream" and stream["status"] == 200)
        frames: list[str] = []
        consumer = asyncio.ensure_future(_collect_frames(stream["content"], frames, limit=3))
        done = await wait_for(lambda: len(frames) >= 3, timeout=2.0)
        consumer.cancel()
        try:
            await consumer
        except (asyncio.CancelledError, StopAsyncIteration):
            pass
        check("J6-2 空闲期收到 ping 帧", done and any("event: ping" in f for f in frames), str(frames))
        check("J6-3 ping 不带业务 seq 且帧格式合法", all('"seq"' not in f for f in frames if "event: ping" in f), str(frames))
        check("J6-4 业务帧仍在 ping 之前重放", bool(frames) and frames[0].startswith("event: text_delta"), frames[:1])
    finally:
        routes.SSE_PING_INTERVAL_SECONDS = real_interval
        await chat_service.abort_generation(gen_id)
        await feed_frames(gen_id, [{"type": "end", "data": "", "streaming": False, "message_id": gen_id}])
        check("J6-5 收尾后任务退出（不泄漏挂起排水）", await wait_for(lambda: entry["task"] is None or entry["task"].done(), timeout=5))


async def _collect_frames(source, frames: list[str], limit: int) -> None:
    """消费 SSE 帧生成器直到取满 limit 帧（测试辅助）。"""
    async for frame in source:
        frames.append(frame)
        if len(frames) >= limit:
            return


# ---------------------------------------------------------------------------
# H. attachment_service.py：防穿越 / 白名单 / 413 / 截断 / 索引 / 孤儿清理
# ---------------------------------------------------------------------------


class FakeUpload(PluginUploadFile):
    """stub：PluginUploadFile 子类（save 按 max_bytes 落盘或抛 UploadTooLargeError）。"""

    def __init__(self, filename: str, content_type: str, payload: bytes) -> None:
        super().__init__(filename=filename, content_type=content_type)
        self.payload = payload
        self.saved_to: Path | None = None
        self.save_max_bytes: int | None = None

    async def save(self, destination, *, max_bytes: int | None = None) -> int:  # noqa: ANN001
        self.saved_to = Path(destination)
        self.save_max_bytes = max_bytes
        if max_bytes is not None and len(self.payload) > max_bytes:
            raise attachment_service.UploadTooLargeError("too large")
        self.saved_to.parent.mkdir(parents=True, exist_ok=True)
        self.saved_to.write_bytes(self.payload)
        return len(self.payload)


def test_attachment_unit() -> None:
    print("== H1. attachment_service 纯函数 ==")
    check("H1-1 防穿越取 basename", attachment_service.sanitize_filename("../../etc/passwd") == "passwd")
    check("H1-2 反斜杠穿越", attachment_service.sanitize_filename("..\\..\\win.ini") == "win.ini")
    try:
        attachment_service.sanitize_filename("")
        check("H1-3 空文件名拒绝", False)
    except attachment_service.InvalidFilenameError:
        check("H1-3 空文件名拒绝", True)
    wl = ["txt", "md", "json", "csv", "py"]
    check(
        "H1-4 classify 图片/文本/未知",
        (
            attachment_service.classify_upload("a.png", "", wl) == "image"
            and attachment_service.classify_upload("a.JPEG", "", wl) == "image"
            and attachment_service.classify_upload("a.webp", "", wl) == "image"
            and attachment_service.classify_upload("a.txt", "", wl) == "text"
            and attachment_service.classify_upload("a.md", "", wl) == "text"
            and attachment_service.classify_upload("a.exe", "", wl) == "unknown"
            and attachment_service.classify_upload("a.md", "", None) == "unknown"
        ),
    )
    check(
        "H1-5 usage_from_agent_stats（AgentStats.to_dict）",
        pipeline_bridge.usage_from_agent_stats({"token_usage": {"input_other": 3, "input_cached": 2, "output": 5}}) == {"output": 5, "total": 10}
        and pipeline_bridge.usage_from_agent_stats(None) == {"output": 0, "total": 0}
        and pipeline_bridge.usage_from_agent_stats({"token_usage": "bad"}) == {"output": 0, "total": 0},
    )
    check(
        "H1-6 媒体帧 data 解析（[IMAGE]/[FILE]display）",
        pipeline_bridge._split_media_payload("image", "[IMAGE]a.png") == ("a.png", None)
        and pipeline_bridge._split_media_payload("file", "[FILE]store.bin|原始名.bin") == ("store.bin", "原始名.bin")
        and pipeline_bridge._split_media_payload("record", "[RECORD]x.wav") == ("x.wav", None)
        and pipeline_bridge._split_media_payload("image", "[IMAGE]../evil.png") == ("evil.png", None),
    )
    check(
        "H1-7 stats_from_agent_stats（cache_rate/字段/max=None）",
        pipeline_bridge.stats_from_agent_stats(
            {"token_usage": {"input_other": 3, "input_cached": 1, "output": 5}, "current_context_tokens": 42}
        ) == {"cache_rate": 0.25, "context_tokens": 42, "max_context_tokens": None, "input_cached": 1, "input_other": 3, "output": 5}
        and pipeline_bridge.stats_from_agent_stats(None) == {"cache_rate": None, "context_tokens": 0, "max_context_tokens": None, "input_cached": 0, "input_other": 0, "output": 0}
        and pipeline_bridge.stats_from_agent_stats({"token_usage": "bad", "current_context_tokens": "9"})["cache_rate"] is None
        and pipeline_bridge.stats_from_agent_stats({"token_usage": {}})["context_tokens"] == 0,
    )
    check(
        "H1-8 read_streaming_response（配置回读与兜底）",
        pipeline_bridge.read_streaming_response(_cfg_ctx({"provider_settings": {"streaming_response": False}})) is False
        and pipeline_bridge.read_streaming_response(_cfg_ctx({"provider_settings": {"streaming_response": True}})) is True
        and pipeline_bridge.read_streaming_response(_cfg_ctx({"provider_settings": {}})) is True
        and pipeline_bridge.read_streaming_response(_cfg_ctx({})) is True
        and pipeline_bridge.read_streaming_response(_cfg_ctx({"provider_settings": {"streaming_response": "yes"}})) is True
        and pipeline_bridge.read_streaming_response(SimpleNamespace()) is True,  # 桩无 get_config
    )


async def _test_attachment_store() -> None:
    print("== H2. store_upload / 宿主附件区 / db 注册 / 索引 / 截断 ==")
    sandbox = fresh_sandbox()
    config = {"attachment_text_whitelist": ["txt", "md"], "upload_image_max_mb": 1, "upload_text_max_kb": 200}
    plugin = make_plugin(sandbox)
    attachments_dir = sandbox / "host-attachments"
    attachments_dir.mkdir(parents=True, exist_ok=True)

    def store(upload):
        return attachment_service.store_upload(upload, plugin.context, config, sandbox, attachments_dir=attachments_dir)

    text_upload = FakeUpload("notes.md", "text/markdown", "# 标题\n正文内容".encode("utf-8"))
    meta_text = await store(text_upload)
    check("H2-1 文本元数据契约键", set(meta_text.keys()) == {"file_id", "kind", "name", "size", "mime", "preview", "truncated"}, str(meta_text.keys()))
    check("H2-2 file_id 前缀", meta_text["file_id"].startswith("f_") and len(meta_text["file_id"]) == 34)
    check("H2-3 truncated False", meta_text["truncated"] is False)
    check("H2-4 preview 前 500 字符", meta_text["preview"].startswith("# 标题"))
    check("H2-5 mime text/markdown", meta_text["mime"] == "text/markdown")
    check("H2-6 落盘宿主附件区（file_id 前缀命名）", text_upload.saved_to is not None and text_upload.saved_to.parent == attachments_dir and text_upload.saved_to.name.startswith(meta_text["file_id"]))
    check("H2-7 磁盘名同源 file_id", f"{meta_text['file_id']}_" in text_upload.saved_to.name)
    check("H2-7b db 注册取得 attachment_id（type=file）", bool(meta_text["file_id"]) and plugin.context.db.attachments and list(plugin.context.db.attachments.values())[0].type == "file", str(plugin.context.db.attachments))

    meta = await attachment_service.get_file(sandbox, meta_text["file_id"])
    check(
        "H2-8 索引登记（含 attachment_id 内部字段 + 绝对路径）",
        meta is not None and meta["cid"] is None and bool(meta.get("attachment_id")) and Path(meta["path"]).is_absolute() and meta["path"].startswith(attachments_dir.resolve().as_posix()),
        str(meta),
    )
    check("H2-9 public_meta 剥离内部字段", "path" not in attachment_service.public_meta(meta) and "cid" not in attachment_service.public_meta(meta) and "attachment_id" not in attachment_service.public_meta(meta))
    check("H2-9b 按 attachment_id 反查", (await attachment_service.find_by_attachment_id(sandbox, meta["attachment_id"]))["file_id"] == meta_text["file_id"])

    png = FakeUpload("pic.png", "image/png", b"\x89PNG\r\n\x1a\nfake")
    meta_img = await store(png)
    check("H2-10 图片 kind/mime", meta_img["kind"] == "image" and meta_img["mime"] == "image/png")
    check("H2-11 图片无 truncated 键", "truncated" not in meta_img)
    check("H2-12 图片 preview 缺省或 data URL（无 PIL 环境降级，真机为 data URL）", "preview" not in meta_img or meta_img["preview"] is None or str(meta_img["preview"]).startswith("data:image/"))
    check("H2-13 图片按 max_bytes 落盘", png.save_max_bytes == 1 * 1024 * 1024)
    meta_img_full = await attachment_service.get_file(sandbox, meta_img["file_id"])
    check("H2-13b db 注册 type=image", meta_img_full is not None and plugin.context.db.attachments.get(meta_img_full.get("attachment_id") or "", None) is not None and getattr(plugin.context.db.attachments[meta_img_full["attachment_id"]], "type", "") == "image", str(meta_img_full))

    # 文本截断：300KB > 200KB -> truncated=True；磁盘保留全文
    big = ("甲" * 200).encode("utf-8") * 1600  # 200 字符 * 3字节 * 1600 ≈ 960KB
    big_upload = FakeUpload("big.txt", "text/plain", big)
    meta_big = await store(big_upload)
    check("H2-14 文本超限 truncated=True", meta_big["truncated"] is True)
    check("H2-15 磁盘保留全文", meta_big["size"] == len(big))
    content, truncated = attachment_service.extract_text_content(big_upload.saved_to, 200)
    check("H2-16 注入内容截断", truncated is True and len(content.encode("utf-8")) <= 200 * 1024)
    small_file = sandbox / "small.txt"
    small_file.write_bytes(b"hi")
    small_content, small_truncated = attachment_service.extract_text_content(small_file, 200)
    check("H2-17 未超限不截断", small_truncated is False and small_content == "hi")

    # 图片超限 -> UploadTooLargeError（routes -> 413）
    over = FakeUpload("big.png", "image/png", b"x" * (1024 * 1024 + 128))
    try:
        await store(over)
        check("H2-18 图片超限 413 前置", False)
    except attachment_service.UploadTooLargeError:
        check("H2-18 图片超限 413 前置", True)

    # 白名单外 -> UnsupportedMediaTypeError（routes -> 415）
    exe = FakeUpload("virus.exe", "application/x-msdownload", b"MZ")
    try:
        await store(exe)
        check("H2-19 白名单外 415 前置", False)
    except attachment_service.UnsupportedMediaTypeError:
        check("H2-19 白名单外 415 前置", True)

    # 引用绑定 / 级联删除
    await attachment_service.bind_file(sandbox, meta_text["file_id"], "c1")
    bound = await attachment_service.get_file(sandbox, meta_text["file_id"])
    check("H2-20 引用绑定 cid", bound["cid"] == "c1")
    removed = await attachment_service.delete_conversation_files(sandbox, "c1")
    check("H2-21 级联删除绑定附件", removed == 1 and await attachment_service.get_file(sandbox, meta_text["file_id"]) is None and not text_upload.saved_to.exists())
    check("H2-22 未绑定附件不受影响", await attachment_service.get_file(sandbox, meta_img["file_id"]) is not None)


async def _test_orphan_sweep() -> None:
    print("== H3. 24h 孤儿清理 ==")
    sandbox = fresh_sandbox()
    now = datetime.now(timezone.utc)
    config = {**_schema_defaults(), "attachment_text_whitelist": ["txt"]}
    plugin = make_plugin(sandbox)
    attachments_dir = sandbox / "host-attachments"
    attachments_dir.mkdir(parents=True, exist_ok=True)

    def store(upload):
        return attachment_service.store_upload(upload, plugin.context, config, sandbox, attachments_dir=attachments_dir)

    old_orphan = await store(FakeUpload("old.txt", "text/plain", b"old"))
    new_orphan = await store(FakeUpload("new.txt", "text/plain", b"new"))
    bound_old = await store(FakeUpload("bound.txt", "text/plain", b"bound"))
    await attachment_service.bind_file(sandbox, bound_old["file_id"], "c1")
    # 把 old_orphan 的登记时间改为 25h 前（sweep 可注入 now，不改全局时钟）
    index = await attachment_service._load_index(sandbox)
    index["files"][old_orphan["file_id"]]["created_at"] = (now - timedelta(hours=25)).isoformat()
    await attachment_service._save_index(sandbox, index)
    old_disk = Path(index["files"][old_orphan["file_id"]]["path"])  # v0.2 索引存绝对路径

    removed = await attachment_service.sweep_orphans(sandbox, now=now)
    check("H3-1 仅清理超 24h 未引用孤儿", removed == 1)
    check("H3-2 孤儿磁盘文件删除", not old_disk.exists() and await attachment_service.get_file(sandbox, old_orphan["file_id"]) is None)
    check("H3-3 新孤儿保留", await attachment_service.get_file(sandbox, new_orphan["file_id"]) is not None)
    check("H3-4 已引用不清理", await attachment_service.get_file(sandbox, bound_old["file_id"]) is not None)

    # 后台任务生命周期：启动 -> 运行中 -> 取消
    await attachment_service.start_orphan_cleanup(plugin)
    task = attachment_service._CLEANUP_TASK
    check("H3-5 清理任务已启动", task is not None and not task.done())
    await attachment_service.start_orphan_cleanup(plugin)  # 幂等
    check("H3-6 幂等启动", attachment_service._CLEANUP_TASK is task)
    await attachment_service.stop_orphan_cleanup()
    check("H3-7 清理任务已取消", task.done())
    await attachment_service.stop_orphan_cleanup()  # 幂等
    check("H3-8 幂等停止", attachment_service._CLEANUP_TASK is None)


async def _test_standalone_logic() -> None:
    print("== L. standalone 鉴权/限速/token 纯逻辑 ==")
    standalone._LOGIN_FAILURES.clear()
    standalone._TOKENS.clear()

    check("L-1 空密码本机放行", standalone.check_password("", "", "127.0.0.1") is True)
    check("L-2 空密码非本机拒绝", standalone.check_password("", "", "192.168.1.5") is False)
    check("L-3 IPv6 回环放行", standalone.check_password("", "", "::1") is True)
    check("L-4 IPv4-mapped 放行", standalone.check_password("", "", "::ffff:127.0.0.1") is True)

    standalone._LOGIN_FAILURES.clear()
    check("L-5 密码正确放行", standalone.check_password("pw", "pw", "10.0.0.1") is True)
    check("L-6 密码错误拒绝", standalone.check_password("bad", "pw", "10.0.0.2") is False)
    for i in range(standalone.LOGIN_MAX_FAILURES - 2):  # 累计 4 败（含 L-6）
        standalone.check_password("bad", "pw", "10.0.0.2")
    check("L-7 未达 5 败未锁定", standalone._login_locked_remaining("10.0.0.2") == 0.0)
    standalone.check_password("bad", "pw", "10.0.0.2")  # 第 5 败 -> 锁定
    check("L-8 5 败锁定", standalone._login_locked_remaining("10.0.0.2") > 0)
    check("L-9 锁定期间密码正确也拒绝", standalone.check_password("pw", "pw", "10.0.0.2") is False)
    other = "10.0.0.3"
    check("L-10 锁定按地址隔离", standalone.check_password("bad", "pw", other) is False)
    standalone.check_password("bad", "pw", other)
    standalone.check_password("bad", "pw", other)
    standalone.check_password("bad", "pw", other)
    standalone.check_password("bad", "pw", other)
    check("L-11 其他地址独立锁定", standalone._login_locked_remaining(other) > 0)

    token = standalone._issue_token()
    check("L-12 token 签发可校验", standalone._verify_token(token) is True)
    check("L-13 未知 token 拒绝", standalone._verify_token("fake") is False)

    # start 在无 aiohttp 环境抛 RuntimeError（真机宿主内置 aiohttp，不会触发）
    try:
        import importlib.util

        if importlib.util.find_spec("aiohttp") is None:
            try:
                await standalone.start(FakePlugin(fresh_sandbox(), {}), SANDBOX, "127.0.0.1", 6186)
                check("L-14 无 aiohttp 时 start 报错", False)
            except RuntimeError:
                check("L-14 无 aiohttp 时 start 报错", True)
        else:
            check("L-14 无 aiohttp 时 start 报错（环境有 aiohttp，跳过）", True)
    except Exception as exc:  # noqa: BLE001
        check("L-14 无 aiohttp 时 start 报错", False, str(exc))
    await standalone.stop(None)
    check("L-15 stop(None) 幂等", True)

    # upload 鉴权（评审修复回归）：/api/upload 与其余 /api/* 一致，先 _verify_token
    # 再分发；无 token/无效 token 必须 401，合法 token 才进入 upload 处理
    class _FakeAioWeb:
        @staticmethod
        def json_response(data, status=200):
            return {"status": status, "data": data}

    class _FakePart:
        def __init__(self) -> None:
            self.name = "file"
            self.filename = "l.txt"
            self.headers = {"Content-Type": "text/plain"}
            self._read = False

        async def read_chunk(self, size):
            if self._read:
                return b""
            self._read = True
            return b"hello"

    class _FakeMultipartReader:
        def __init__(self, part) -> None:
            self._part = part

        async def next(self):
            part, self._part = self._part, None
            return part

    class _FakeAioRequest:
        def __init__(self, endpoint, headers=None, query=None, method="POST", content_type="multipart/form-data; boundary=x"):
            self.match_info = {"endpoint": endpoint}
            self.method = method
            self.headers = headers or {}
            self.query = query or {}
            self.content_type = content_type

        async def multipart(self):
            return _FakeMultipartReader(_FakePart())

    sandbox_l = fresh_sandbox()
    set_stub_host_data(sandbox_l)
    plugin_l = make_plugin(sandbox_l)
    auth_headers = {"Authorization": f"Bearer {standalone._issue_token()}"}

    resp = await standalone._handle_api(_FakeAioRequest("upload"), plugin_l, _FakeAioWeb())
    check("L-16 无 token POST /api/upload 401", resp["status"] == 401 and resp["data"]["error"] == "unauthorized", str(resp))
    resp = await standalone._handle_api(_FakeAioRequest("upload", headers={"Authorization": "Bearer bad"}), plugin_l, _FakeAioWeb())
    check("L-17 无效 token 401", resp["status"] == 401 and resp["data"]["error"] == "unauthorized", str(resp))
    resp = await standalone._handle_api(_FakeAioRequest("upload", headers=auth_headers), plugin_l, _FakeAioWeb())
    check("L-18 带 token 上传 2xx（multipart 元数据）", resp["status"] == 200 and resp["data"]["kind"] == "text" and resp["data"]["file_id"].startswith("f_"), str(resp))
    check("L-19 带 token 落盘宿主附件区 + db 注册", bool(plugin_l.context.db.attachments), str(plugin_l.context.db.attachments))
    resp = await standalone._handle_api(_FakeAioRequest("health"), plugin_l, _FakeAioWeb())
    check("L-20 其余 /api/* 端点无 token 依旧 401", resp["status"] == 401, str(resp))

    # v0.3.1：ui/settings 端点已入独立端口路由表（GET/POST 精确匹配）
    import re as _re

    get_match = [name for m, pat, name in standalone._API_ROUTES if m == "GET" and _re.fullmatch(pat, "ui/settings")]
    post_match = [name for m, pat, name in standalone._API_ROUTES if m == "POST" and _re.fullmatch(pat, "ui/settings")]
    check("L-21 GET ui/settings 入表", get_match == ["handle_get_ui_settings"], str(get_match))
    check("L-22 POST ui/settings 入表", post_match == ["handle_post_ui_settings"], str(post_match))
    config_match = [name for m, pat, name in standalone._API_ROUTES if m == "GET" and _re.fullmatch(pat, "config")]
    check("L-23 GET config 入表（v0.3.6）", config_match == ["handle_get_config"], str(config_match))


# ---------------------------------------------------------------------------
# E. routes.py：精确路由注册 / 错误包 / 端点行为
# ---------------------------------------------------------------------------


def test_routes() -> None:
    print("== E. routes 路由表与错误包 ==")
    sandbox = fresh_sandbox()
    chat_service._GENERATIONS.clear()
    plugin = make_plugin(sandbox)
    register = routes.register_routes
    register(plugin)
    registered: dict[str, list[str]] = {}
    handlers: dict[str, object] = {}
    for route, view, methods, _desc in plugin.context.routes:
        registered.setdefault(route, [])
        registered[route].extend(methods)
        handlers[(route, tuple(sorted(methods)))] = view

    expected = {
        "/astrbot_plugin_webui/health": ["GET"],
        "/astrbot_plugin_webui/config": ["GET"],
        "/astrbot_plugin_webui/personas": ["GET"],
        "/astrbot_plugin_webui/conversations": ["GET", "POST"],
        "/astrbot_plugin_webui/conversations/<cid>": ["DELETE"],
        "/astrbot_plugin_webui/conversations/<cid>/delete": ["POST"],
        "/astrbot_plugin_webui/history/<cid>": ["GET"],
        "/astrbot_plugin_webui/send": ["POST"],
        "/astrbot_plugin_webui/stop/<gen_id>": ["POST"],
        "/astrbot_plugin_webui/stream/<gen_id>": ["GET"],
        "/astrbot_plugin_webui/upload": ["POST"],
        "/astrbot_plugin_webui/files/<file_id>": ["GET"],
        "/astrbot_plugin_webui/ui/settings": ["GET", "POST"],
    }
    check("E1 逐端点精确注册", registered == expected, str(registered))
    check("E2 无 <route> 通配", "/astrbot_plugin_webui/<route>" not in registered)

    async def call(route: str, method: str, **kwargs):
        return await handlers[(route, tuple(sorted([method])))](**kwargs)

    async def _run() -> None:
        # health
        resp = await call("/astrbot_plugin_webui/health", "GET")
        check("E3 health 契约", resp["__type"] == "json" and resp["status"] == 200 and resp["data"] == {"status": "ok", "plugin": "astrbot_plugin_webui", "version": "v9.9.9-test"}, str(resp))

        # personas
        resp = await call("/astrbot_plugin_webui/personas", "GET")
        data = resp["data"]
        check("E4 personas 键", set(data.keys()) == {"image_capable", "personas"} and data["image_capable"] is True)
        by_id = {p["id"]: p for p in data["personas"]}
        check("E5 id==name==persona_id", by_id["p1"]["id"] == by_id["p1"]["name"] == "p1")
        check("E6 预览<=80", len(by_id["p2"]["system_prompt_preview"]) <= 81)
        check("E7 is_default 比对", by_id["p1"]["is_default"] is True and by_id["p2"]["is_default"] is False)
        check("E8 has_tools 语义", (by_id["p1"]["has_tools"], by_id["p2"]["has_tools"], by_id["p3"]["has_tools"]) == (True, False, True))

        # conversations: create
        stub_request.set_json({"persona_id": "p1"})
        resp = await call("/astrbot_plugin_webui/conversations", "POST")
        cid = resp["data"]["cid"]
        check("E9 create -> {cid}", resp["status"] == 200 and len(cid) == 32)
        stub_request.set_json({"persona_id": "不存在"})
        resp = await call("/astrbot_plugin_webui/conversations", "POST")
        check("E10 非法 persona 400", resp["status"] == 400 and resp["data"]["error"] == "invalid_request" and resp["data"]["code"] == 400)
        check("E11 错误包形状", set(resp["data"].keys()) == {"code", "error", "message"})
        stub_request.set_json(None)
        resp = await call("/astrbot_plugin_webui/conversations", "POST")
        check("E12 非 JSON body 400", resp["status"] == 400 and resp["data"]["error"] == "invalid_request")

        # conversations: list + busy + gen_id
        stub_request.set_json(None)
        resp = await call("/astrbot_plugin_webui/conversations", "GET")
        items = resp["data"]["conversations"]
        check("E13 会话列表 busy/gen_id 键", all(set(i.keys()) == {"cid", "title", "persona_id", "updated_at", "busy", "gen_id"} for i in items), str(items[0].keys() if items else None))
        check("E13b 无任务时 gen_id null", all(i["gen_id"] is None and i["busy"] is False for i in items))
        chat_service._GENERATIONS["fake-gen"] = {"gen_id": "fake-gen", "cid": cid, "task": None, "buffer": sse.new_buffer(), "waiters": set(), "finished": False, "finished_at": None, "last_seq": -1}
        resp = await call("/astrbot_plugin_webui/conversations", "GET")
        items = resp["data"]["conversations"]
        busy_map = {i["cid"]: i["busy"] for i in items}
        gen_map = {i["cid"]: i["gen_id"] for i in items}
        check("E14 busy=该 cid 有进行中任务", busy_map.get(cid) is True)
        check("E14b 有任务项 gen_id 一致", gen_map.get(cid) == "fake-gen" and busy_map.get(cid) is True, str(gen_map))
        check("E14c 其余会话 gen_id null", all(v is None for k, v in gen_map.items() if k != cid))
        chat_service._GENERATIONS.clear()

        # history（新会话读平台历史；桩 db 空历史 -> 空 messages）
        stub_request.set_query([("page", "1"), ("size", "999")])
        resp = await call("/astrbot_plugin_webui/history/<cid>", "GET", cid=cid)
        data = resp["data"]
        check("E15 history 契约键", set(data.keys()) == {"cid", "page", "size", "total", "has_more", "messages"})
        check("E16 size 上限 100", data["size"] == 100)
        stub_request.set_query([("page", "0")])
        resp = await call("/astrbot_plugin_webui/history/<cid>", "GET", cid=cid)
        check("E17 page<1 钳制", resp["data"]["page"] == 1)
        stub_request.set_query([])
        resp = await call("/astrbot_plugin_webui/history/<cid>", "GET", cid="missing")
        check("E18 未知会话 404", resp["status"] == 404 and resp["data"]["error"] == "not_found")

        # send 校验
        stub_request.set_json({"cid": cid, "text": "", "file_ids": []})
        resp = await call("/astrbot_plugin_webui/send", "POST")
        check("E19 空内容 400", resp["status"] == 400 and resp["data"]["error"] == "invalid_request")
        stub_request.set_json({"cid": cid, "text": "带附件", "file_ids": ["f_x"]})
        resp = await call("/astrbot_plugin_webui/send", "POST")
        check("E20 未知 file_ids 明确 400", resp["status"] == 400)
        stub_request.set_json({"cid": "missing", "text": "hi"})
        resp = await call("/astrbot_plugin_webui/send", "POST")
        check("E21 未知 cid 404", resp["status"] == 404 and resp["data"]["error"] == "not_found")

        # send 正常（桩 webchat 队列注入成功；排水阻塞等待载荷，任务保持进行中）
        stub_request.set_json({"cid": cid, "text": "你好"})
        resp = await call("/astrbot_plugin_webui/send", "POST")
        gen_id = resp["data"].get("gen_id", "")
        check("E22 send 恒 200 {gen_id}", resp["status"] == 200 and len(gen_id) == 32)
        stub_request.set_json({"cid": cid, "text": "第二条"})
        resp = await call("/astrbot_plugin_webui/send", "POST")
        check("E23 session_busy 409", resp["status"] == 409 and resp["data"]["error"] == "session_busy" and resp["data"]["code"] == 409)

        # stop（先落终态，不再取消消费任务）
        stub_request.set_json(None)
        resp = await call("/astrbot_plugin_webui/stop/<gen_id>", "POST", gen_id=gen_id)
        check("E24 stop -> ok/stopped", resp["status"] == 200 and resp["data"] == {"ok": True, "stopped": True})
        ok_stopped = await wait_for(lambda: any(e["event"] == "stopped" for e in chat_service._GENERATIONS.get(gen_id, {}).get("buffer", [])))
        check("E24b stopped 终态已入缓冲", ok_stopped)
        resp = await call("/astrbot_plugin_webui/stop/<gen_id>", "POST", gen_id=gen_id)
        check("E25 已结束 stop 404", resp["status"] == 404)

        stream_resp = await call("/astrbot_plugin_webui/stream/<gen_id>", "GET", gen_id=gen_id)
        check("E26 stream 200 + event-stream", stream_resp["__type"] == "stream" and stream_resp["status"] == 200 and stream_resp["content_type"] == "text/event-stream", str(stream_resp))

        # stream 内容重放（消费 SSE 生成器；终态后关流）
        frames: list[str] = []
        async for frame in stream_resp["content"]:
            frames.append(frame)
        check("E28 SSE 帧含终态", any("event: stopped" in f for f in frames))
        check("E29 SSE data 含 seq", all('"seq": ' in f for f in frames))

        resp = await call("/astrbot_plugin_webui/stream/<gen_id>", "GET", gen_id="missing")
        check("E27 未知 gen stream 404", resp["status"] == 404)

        # delete（POST 别名）幂等 + 级联（取消消费任务 + 宿主级联）
        stub_request.set_json(None)
        resp = await call("/astrbot_plugin_webui/conversations/<cid>/delete", "POST", cid=cid)
        check("E30 delete 别名 ok", resp["status"] == 200 and resp["data"] == {"ok": True})
        resp2 = await call("/astrbot_plugin_webui/conversations/<cid>/delete", "POST", cid=cid)
        check("E31 delete 幂等", resp2["status"] == 200)
        check("E32 级联删宿主会话", umo_of(cid) in plugin.context.conversation_manager.deleted_by_user)
        check("E33 级联删 platform_session", cid in plugin.context.db.deleted_sessions)
        check("E34 DELETE 等价", (await call("/astrbot_plugin_webui/conversations/<cid>", "DELETE", cid=cid))["status"] == 200)
        ok_task_done = await wait_for(lambda: chat_service._GENERATIONS.get(gen_id, {}).get("task") is None or chat_service._GENERATIONS[gen_id]["task"].done())
        check("E35 消费任务已随删除取消", ok_task_done)

        # 端到端：POST send -> 注入 -> 脚本化排水 -> done + bot 平台历史
        stub_request.set_json({"persona_id": None})
        resp = await call("/astrbot_plugin_webui/conversations", "POST")
        cid2 = resp["data"]["cid"]
        stub_request.set_json({"cid": cid2, "text": "端到端"})
        resp = await call("/astrbot_plugin_webui/send", "POST")
        gen2 = resp["data"]["gen_id"]
        await feed_frames(gen2, _std_frames(gen2))
        entry2 = chat_service._GENERATIONS[gen2]
        check("E36 send→注入→排水→done 全链", await wait_for(lambda: entry2["task"].done() and any(e["event"] == "done" for e in entry2["buffer"]), timeout=10))
        check("E37 done 后 active=None", chat_service.active_generation_id(cid2) is None)
        check("E38 bot 平台历史落库", any(r.content.get("type") == "bot" for r in plugin.context.message_history_manager.rows))
        check("E39 队列已清理", gen2 not in _stub_queue_mgr().back_queues)

        # ── v0.3.1 UI 设置端点（GET/POST ui/settings；头像落盘 User_head/LLM_head.png） ──
        import base64 as _base64

        stub_request.set_json(None)
        resp = await call("/astrbot_plugin_webui/ui/settings", "GET")
        check("E40 GET ui/settings 空态", resp["status"] == 200 and resp["data"]["settings"] == {"avatarUser": "", "avatarAssistant": ""}, str(resp))
        png_url = "data:image/png;base64," + _base64.b64encode(b"fakepng").decode("ascii")
        stub_request.set_json({"compact": True, "flowWidth": "wide", "avatarUser": png_url, "avatarAssistant": ""})
        resp = await call("/astrbot_plugin_webui/ui/settings", "POST")
        check("E41 POST ui/settings ok", resp["status"] == 200 and resp["data"]["ok"] is True, str(resp))
        check("E42 头像落盘固定文件名", (sandbox / "User_head.png").is_file() and not (sandbox / "LLM_head.png").exists())
        check("E43 响应回填头像与设置", resp["data"]["settings"]["avatarUser"] == png_url and resp["data"]["settings"]["compact"] is True and resp["data"]["settings"]["flowWidth"] == "wide", str(resp["data"]))
        stub_request.set_json(None)
        resp = await call("/astrbot_plugin_webui/ui/settings", "GET")
        check("E44 GET 从文件回填头像", resp["data"]["settings"]["avatarUser"] == png_url and resp["data"]["settings"]["compact"] is True and resp["data"]["settings"]["avatarAssistant"] == "", str(resp["data"]))
        stored = json.loads((sandbox / "ui_settings.json").read_text(encoding="utf-8"))
        check("E45 设置 JSON 不落头像数据", stored["avatarUser"] == "" and stored["compact"] is True, str(stored))
        stub_request.set_json({"avatarUser": "data:image/jpeg;base64,AAAA"})
        resp = await call("/astrbot_plugin_webui/ui/settings", "POST")
        check("E46 非 PNG data URL 400", resp["status"] == 400 and resp["data"]["error"] == "invalid_request", str(resp))
        stub_request.set_json({"avatarUser": 123})
        resp = await call("/astrbot_plugin_webui/ui/settings", "POST")
        check("E47 非字符串头像 400", resp["status"] == 400 and resp["data"]["error"] == "invalid_request")
        stub_request.set_json({"compact": False, "avatarUser": ""})
        resp = await call("/astrbot_plugin_webui/ui/settings", "POST")
        check("E48 空串清除头像删文件", resp["status"] == 200 and resp["data"]["settings"]["avatarUser"] == "" and not (sandbox / "User_head.png").exists(), str(resp))
        stub_request.set_json({"compact": False})
        resp = await call("/astrbot_plugin_webui/ui/settings", "POST")
        check("E49 头像键缺省按空串处理", resp["status"] == 200 and resp["data"]["settings"]["avatarUser"] == "" and resp["data"]["settings"]["compact"] is False)

        # ── v0.3.6 展示配置端点（GET config → user_display_name，实时读配置） ──
        stub_request.set_json(None)
        resp = await call("/astrbot_plugin_webui/config", "GET")
        check("E50 GET config 默认 USER", resp["status"] == 200 and resp["data"]["user_display_name"] == "USER", str(resp))
        plugin.config["user_display_name"] = "泡泡"
        resp = await call("/astrbot_plugin_webui/config", "GET")
        check("E51 GET config 实时读配置", resp["status"] == 200 and resp["data"]["user_display_name"] == "泡泡", str(resp))
        plugin.config["user_display_name"] = "USER"

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# K. routes：upload / files 端点 + send 附件链路 + 级联删除
# ---------------------------------------------------------------------------


async def _test_routes_attachments() -> None:
    print("== K. routes 附件端点与 send 附件链路 ==")
    chat_service._GENERATIONS.clear()
    sandbox = fresh_sandbox()
    # 桩宿主数据路径注入沙盒（handle_upload 落点 = <stub data>/attachments/）
    from astrbot.core.utils import astrbot_path as stub_astrbot_path

    stub_astrbot_path.set_stub_data_path(sandbox / "hostdata")
    config = {"attachment_text_whitelist": ["txt", "md"], "upload_image_max_mb": 1, "upload_text_max_kb": 200}
    plugin = make_plugin(sandbox, config=config)

    register = routes.register_routes
    register(plugin)
    handlers: dict[tuple[str, tuple[str, ...]], object] = {}
    for route, view, methods, _desc in plugin.context.routes:
        handlers[(route, tuple(sorted(methods)))] = view

    async def call(route: str, method: str, **kwargs):
        return await handlers[(route, tuple(sorted([method])))](**kwargs)

    # upload：无文件 400
    stub_request.set_files({})
    resp = await call("/astrbot_plugin_webui/upload", "POST")
    check("K-1 无文件 400", resp["status"] == 400 and resp["data"]["error"] == "invalid_request")

    # upload：文本
    text_upload = FakeUpload("a.md", "text/markdown", "# 标题\n附件正文".encode("utf-8"))
    stub_request.set_files({"file": text_upload})
    resp = await call("/astrbot_plugin_webui/upload", "POST")
    check("K-2 upload 文本 200 元数据", resp["status"] == 200 and resp["data"]["kind"] == "text" and resp["data"]["file_id"].startswith("f_"), str(resp["data"]))
    fid_text = resp["data"]["file_id"]
    check("K-2b 落盘 stub 宿主附件区", text_upload.saved_to is not None and text_upload.saved_to.parent == sandbox / "hostdata" / "attachments", str(text_upload.saved_to))

    # upload：图片（无 PIL 环境 preview 允许 None；真机为 data URL）
    png_upload = FakeUpload("b.png", "image/png", b"\x89PNG fake")
    stub_request.set_files({"file": png_upload})
    resp = await call("/astrbot_plugin_webui/upload", "POST")
    check("K-3 upload 图片元数据", resp["status"] == 200 and resp["data"]["kind"] == "image" and resp["data"]["mime"] == "image/png" and "truncated" not in resp["data"])
    fid_img = resp["data"]["file_id"]
    check("K-3b db 注册取得 attachment_id", bool(plugin.context.db.attachments), str(plugin.context.db.attachments))

    # upload：白名单外 415 / 图片超限 413
    stub_request.set_files({"file": FakeUpload("x.exe", "application/x-msdownload", b"MZ")})
    resp = await call("/astrbot_plugin_webui/upload", "POST")
    check("K-4 415 unsupported_media_type", resp["status"] == 415 and resp["data"]["error"] == "unsupported_media_type" and resp["data"]["code"] == 415)
    stub_request.set_files({"file": FakeUpload("big.png", "image/png", b"x" * (1024 * 1024 + 64))})
    resp = await call("/astrbot_plugin_webui/upload", "POST")
    check("K-5 413 payload_too_large", resp["status"] == 413 and resp["data"]["error"] == "payload_too_large")

    # files/<file_id>：原文件 + 404
    stub_request.set_files({})
    resp = await call("/astrbot_plugin_webui/files/<file_id>", "GET", file_id=fid_img)
    check("K-6 files 返回 file_response", resp["__type"] == "file" and resp["path"].endswith(".png") and resp["filename"] is None, str(resp))
    resp = await call("/astrbot_plugin_webui/files/<file_id>", "GET", file_id="f_missing")
    check("K-7 未知 file_id 404", resp["status"] == 404)

    # send + file_ids 端到端（v0.2 内核：附件按 attachment_id 注入，宿主管道消费）
    stub_request.set_json({"persona_id": None})
    resp = await call("/astrbot_plugin_webui/conversations", "POST")
    cid = resp["data"]["cid"]
    stub_request.set_json({"cid": cid, "text": "看看这两个附件", "file_ids": [fid_text, fid_img]})
    resp = await call("/astrbot_plugin_webui/send", "POST")
    gen_id = resp["data"].get("gen_id", "")
    check("K-8 send 恒 200 {gen_id}", resp["status"] == 200 and len(gen_id) == 32)

    # 注入 payload parts 校验（经桩队列捕获；等生成任务完成注入三步）
    check("K-8b 注入落地", await wait_for(lambda: cid in _stub_queue_mgr().queues and bool(plugin.context.message_history_manager.rows), timeout=2))
    queued = await asyncio.wait_for(_stub_queue_mgr().queues[cid].get(), timeout=2)
    payload = queued[2]
    types = [p["type"] for p in payload["message"]]
    check("K-9 payload parts 含 plain+image+file", types[0] == "plain" and "image" in types and "file" in types, str(types))
    full_img = await attachment_service.get_file(plugin.data_dir(), fid_img)
    img_part = next(p for p in payload["message"] if p["type"] == "image")
    check("K-10 image part 携带 attachment_id+path", img_part.get("attachment_id") == full_img["attachment_id"] and "path" in img_part, str(img_part))

    # 排水收尾（空 end：无 bot 行），断言任务收尾与队列纪律
    await feed_frames(gen_id, [{"type": "end", "data": "", "streaming": False, "message_id": gen_id}])
    entry = chat_service._GENERATIONS[gen_id]
    check("K-11 排水收尾", await wait_for(lambda: entry["task"].done(), timeout=5))

    # 用户平台历史：媒体 parts 落库（path 剥除）；索引引用绑定
    user_rows = [r for r in plugin.context.message_history_manager.rows if r.content.get("type") == "user"]
    media_parts = [p for p in user_rows[0].content["message"] if p["type"] in ("image", "file")]
    check("K-12 用户媒体 parts 落库且剥 path", len(media_parts) == 2 and all("path" not in p and p.get("attachment_id") for p in media_parts), str(media_parts))
    bound_text = await attachment_service.get_file(plugin.data_dir(), fid_text)
    bound_img = await attachment_service.get_file(plugin.data_dir(), fid_img)
    check("K-13 索引引用绑定 cid", bound_text["cid"] == cid and bound_img["cid"] == cid)

    # history 端点：attachments 经索引反查（与用户消息 attachments 同构）
    stub_request.set_query([("page", "1"), ("size", "30")])
    resp = await call("/astrbot_plugin_webui/history/<cid>", "GET", cid=cid)
    msgs = resp["data"]["messages"]
    check("K-14 history user attachments 反查", [a["file_id"] for a in msgs[0].get("attachments") or []] == [fid_text, fid_img], str(msgs[0]))

    # send：未知 file_id 400；仅附件无文本 200
    stub_request.set_json({"cid": cid, "text": "", "file_ids": ["f_missing"]})
    resp = await call("/astrbot_plugin_webui/send", "POST")
    check("K-15 未知 file_id 400", resp["status"] == 400)
    stub_request.set_json({"cid": cid, "text": "", "file_ids": [fid_text]})
    resp = await call("/astrbot_plugin_webui/send", "POST")
    gen2 = resp["data"]["gen_id"]
    check("K-16 纯附件发送 200", resp["status"] == 200 and len(gen2) == 32)
    await feed_frames(gen2, [{"type": "end", "data": "", "streaming": False, "message_id": gen2}])
    check("K-16b 纯附件任务收尾", await wait_for(lambda: chat_service._GENERATIONS[gen2]["task"].done(), timeout=5))

    # 删除会话级联：附件 db 行 + 磁盘 + 索引 + 平台历史 + 队列
    db_count_before = len(plugin.context.db.attachments)
    stub_request.set_json(None)
    resp = await call("/astrbot_plugin_webui/conversations/<cid>/delete", "POST", cid=cid)
    check("K-17 删除会话 ok", resp["status"] == 200 and resp["data"] == {"ok": True})
    check("K-18 附件索引级联删除", await attachment_service.get_file(plugin.data_dir(), fid_text) is None and await attachment_service.get_file(plugin.data_dir(), fid_img) is None)
    check("K-19 附件磁盘删除", not text_upload.saved_to.exists() and not png_upload.saved_to.exists())
    check("K-20 db 附件行级联删除", len(plugin.context.db.attachments) == db_count_before - 2, str(len(plugin.context.db.attachments)))
    check("K-21 平台历史清空", plugin.context.message_history_manager.rows == [])
    check("K-22 级联 offset_sec=99999999", any(d["offset_sec"] == 99999999 and d["user_id"] == cid for d in plugin.context.message_history_manager.deleted_offsets))
    check("K-23 remove_queues", cid not in _stub_queue_mgr().queues)


# ---------------------------------------------------------------------------
# F. 仍为桩的面：NotImplementedError 桩契约（当前无桩面，骨架保留）
# ---------------------------------------------------------------------------

STUB_CASES: list[tuple[str, object, tuple, str]] = [
    # M3 评审修复后：standalone 已实现（鉴权/限速/token 为纯逻辑见 L 组；start/stop
    # 延迟导入 aiohttp，无 aiohttp 环境行为见 L-14）——当前无桩面，列表保留空态。
]


def test_stub_surfaces() -> None:
    print("== F. 未实现面保持 NotImplementedError 桩契约（当前空态） ==")

    async def run_async(func, args):
        await func(*args)

    for desc, func, args, kind in STUB_CASES:
        try:
            if kind == "sync":
                func(*args)
            else:
                asyncio.run(run_async(func, args))
        except NotImplementedError:
            check(desc, True)
        except Exception as exc:  # noqa: BLE001
            check(desc, False, f"抛出了 {type(exc).__name__}: {exc}")
        else:
            check(desc, False, "未抛出 NotImplementedError（骨架契约被提前实现？）")


def check_docstrings() -> None:
    """core/ 全部公开函数保留 docstring（M1 契约延续）。"""
    print("== G. docstring 契约 ==")
    modules = (
        routes, persona_service, conversation_service, chat_service,
        attachment_service, pipeline_bridge, uistore, sse, standalone,
    )
    bad: list[str] = []
    for module in modules:
        for name, obj in inspect.getmembers(module, inspect.isfunction):
            if name.startswith("_"):
                continue
            if getattr(obj, "__module__", "") != module.__name__:
                continue
            if not obj.__doc__:
                bad.append(f"{module.__name__}.{name}")
    check("G1 公开函数均有 docstring", not bad, ", ".join(bad))


def main() -> int:
    sweep_stale_sandboxes()
    test_sse()
    asyncio.run(_test_uistore())
    asyncio.run(_test_conversation_service())
    asyncio.run(_test_chat_happy_path())
    asyncio.run(_test_chat_session_busy())
    asyncio.run(_test_chat_concurrency())
    asyncio.run(_test_chat_abort())
    asyncio.run(_test_chat_idle_timeout())
    asyncio.run(_test_chat_queue_discipline())
    asyncio.run(_test_chat_media_frames())
    asyncio.run(_test_streaming_flags())
    asyncio.run(_test_chat_concurrent_send())
    asyncio.run(_test_delete_while_generating())
    asyncio.run(_test_sse_ping())
    test_attachment_unit()
    asyncio.run(_test_attachment_store())
    asyncio.run(_test_orphan_sweep())
    asyncio.run(_test_standalone_logic())
    test_routes()
    asyncio.run(_test_routes_attachments())
    test_stub_surfaces()
    check_docstrings()

    total = PASSED + len(FAILURES)
    print(f"\nselftest: {total} 项断言，通过 {PASSED}，失败 {len(FAILURES)}")
    if FAILURES:
        for f in FAILURES:
            print(f"  FAIL  {f}")
        return 1
    print("selftest 全部通过")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(SANDBOX, ignore_errors=True)
