#!/usr/bin/env python3
"""M2 自测：stub 宿主下验证对话与流式全链路（UserPlan §7-M2 + 对接契约 v2）。

遵循 astrbot-plugin-dev skill references/09 的「stub 宿主自测法」：

- ``sys.path`` **先桩**（tests/stub 的假 astrbot 包）**后本体**（插件根目录，
  core/ 作为顶层包导入）；main.py 依赖真实宿主运行时注入，桩下不可导入，
  因此只测 core/；
- M2 覆盖点：SSE 缓冲重放（幂等）、uistore 每 cid 原子写与分页、umo 构造、
  配置默认值实时读取（并发上限热生效）、同会话 single-flight 409、abort 后
  重订阅见 stopped 终态、历史双写一致、路由表精确注册、契约错误包形状；
- 当前 core/ 无桩面（standalone 已于评审修复轮实现：鉴权/限速/token 纯逻辑见
  L 组，start/stop 延迟导入 aiohttp，无 aiohttp 环境行为见 L-14）；
  F 组保留空态骨架以备后续里程碑回填。

用法（工作区任意目录均可）::

    .venv/bin/python.exe tests/selftest.py

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
import core.routes as routes  # noqa: E402
import core.sse as sse  # noqa: E402
import core.standalone as standalone  # noqa: E402
import core.tool_loop as tool_loop  # noqa: E402
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
# 桩宿主：假 provider / conversation_manager / persona_manager / context / plugin
# ---------------------------------------------------------------------------


class FakeChain:
    """stub：MessageChain 等价物（get_plain_text 提取增量文本）。"""

    def __init__(self, *texts: str) -> None:
        self.texts = list(texts)

    def get_plain_text(self) -> str:
        return "".join(self.texts)


def _chunk(text: str | None = None, reasoning: str | None = None) -> SimpleNamespace:
    """流式增量 chunk（is_chunk=True）。"""
    return SimpleNamespace(
        is_chunk=True,
        result_chain=FakeChain(text) if text is not None else None,
        reasoning_content=reasoning,
        completion_text=None,
        reasoning_signature=None,
        usage=None,
    )


def _final(
    completion_text: str,
    reasoning_content: str | None = None,
    usage: dict | None = None,
    reasoning_signature: str | None = None,
) -> SimpleNamespace:
    """末次完整响应（is_chunk=False）。"""
    return SimpleNamespace(
        is_chunk=False,
        result_chain=FakeChain(completion_text),
        reasoning_content=reasoning_content,
        completion_text=completion_text,
        reasoning_signature=reasoning_signature,
        usage=usage,
    )


class FakeProvider:
    """stub：text_chat_stream 适配层唯一触碰点（脚本化产出 chunk 序列）。"""

    def __init__(self, scripts: list[list]) -> None:
        self.scripts = list(scripts)
        self.calls: list[dict] = []
        self.gate: asyncio.Event | None = None  # 置位后慢速生成器才继续/收尾

    def text_chat_stream(self, **kwargs):
        self.calls.append(kwargs)
        script = self.scripts.pop(0) if self.scripts else []
        gate = self.gate
        return self._gen(script, gate)

    async def _gen(self, script: list, gate: asyncio.Event | None):
        for item in script:
            if isinstance(item, type(asyncio.Event())):  # 等待闸门（慢速模拟）
                if gate is not None:
                    await gate.wait()
                continue
            yield item


class FakeConversationManager:
    """stub：conversation_manager（内存会话表 + 调用记录）。"""

    def __init__(self) -> None:
        self.convs: dict[str, SimpleNamespace] = {}
        self.title_updates: list[tuple[str, str]] = []
        self.pairs: list[tuple[str, dict, dict]] = []
        self.deleted: list[str] = []

    def seed(self, cid: str, persona_id: str | None = None, updated_at: int = 1000) -> SimpleNamespace:
        conv = SimpleNamespace(
            platform_id="webui",
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
        cid = uuid.uuid4().hex
        conv = self.seed(cid, persona_id=persona_id)
        conv.user_id = unified_msg_origin
        conv.title = title or "新会话"
        return cid

    async def get_conversations(self, unified_msg_origin=None, platform_id=None):
        return [
            conv
            for conv in self.convs.values()
            if platform_id is None or conv.platform_id == platform_id
        ]

    async def get_conversation(self, unified_msg_origin, conversation_id, create_if_not_exists=False):
        return self.convs.get(conversation_id)

    async def delete_conversation(self, unified_msg_origin, conversation_id=None):
        if conversation_id:
            self.convs.pop(conversation_id, None)
            self.deleted.append(conversation_id)

    async def update_conversation(self, unified_msg_origin, conversation_id=None, history=None, title=None, persona_id=None, token_usage=None):
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


class FakeTool:
    """stub：FunctionTool 兼容工具对象（references/04 的 call(context, **kwargs) 接口）。"""

    def __init__(self, name: str, result: str = "ok", error: str | None = None) -> None:
        self.name = name
        self.result = result
        self.error = error
        self.calls: list[dict] = []

    async def call(self, context, **kwargs):  # noqa: ANN001
        self.calls.append(kwargs)
        if self.error:
            raise RuntimeError(self.error)
        return self.result


class FakeToolManager:
    """stub：宿主 LLM 工具管理器（func_list 成员，references/04 实证）。"""

    def __init__(self, tools: list) -> None:
        self.func_list = list(tools)


class FakeContext:
    """stub：宿主上下文（捕获 register_web_api 路由表 + 工具管理器/方案 B 入口）。"""

    def __init__(self) -> None:
        self.routes: list[tuple[str, object, list[str], str]] = []
        self.conversation_manager = FakeConversationManager()
        self.persona_manager = FakePersonaManager([], "default")
        self.provider = None
        self.tool_manager: FakeToolManager | None = None
        self.agent_calls: list[dict] = []
        self._stars = {"astrbot_plugin_webui": SimpleNamespace(version="v9.9.9-test")}

    def register_web_api(self, route, view_handler, methods, desc) -> None:
        self.routes.append((route, view_handler, list(methods), desc))

    def get_using_provider(self, umo=None):
        return self.provider

    def get_registered_star(self, name):
        return self._stars.get(name)

    def get_llm_tool_manager(self):
        if self.tool_manager is None:
            raise AttributeError("tool manager 未配置")
        return self.tool_manager

    async def get_current_chat_provider_id(self, umo=None):
        return "prov-1"

    async def tool_loop_agent(self, event=None, chat_provider_id=None, prompt=None, system_prompt=None, contexts=None, tools=None, image_urls=None, agent_hooks=None):
        """stub：方案 B 入口（触发钩子并返回最终响应）。"""
        self.agent_calls.append(
            {
                "event": event,
                "chat_provider_id": chat_provider_id,
                "prompt": prompt,
                "tools": tools,
                "image_urls": image_urls,
                "agent_hooks": agent_hooks,
            }
        )
        if agent_hooks is not None:
            await agent_hooks["on_using_llm_tool"](None, None, "get_weather", {"location": "北京"})
            await agent_hooks["on_llm_tool_respond"](None, None, "get_weather", {"location": "北京"}, "晴 26℃")
        return SimpleNamespace(
            is_chunk=False,
            result_chain=FakeChain("宿主循环答案"),
            completion_text="宿主循环答案",
            reasoning_content=None,
            reasoning_signature=None,
            usage={"output": 4, "total": 6},
        )


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


def make_plugin(sandbox: Path, provider: FakeProvider | None, config: dict | None = None) -> FakePlugin:
    base = _schema_defaults()  # 代码不再有第二重默认值兜底，测试配置必须含全部键
    base.update(config or {})
    plugin = FakePlugin(sandbox, base)
    plugin.context.provider = provider
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


async def await_task(task: asyncio.Task) -> None:
    """等待任务收尾并吸收 CancelledError（被中止任务的正常归宿）。"""
    try:
        await task
    except asyncio.CancelledError:
        pass


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
# B. uistore.py：每 cid JSON / 原子写 / 版本字段 / 分页
# ---------------------------------------------------------------------------


async def _test_uistore() -> None:
    print("== B. uistore 每 cid 文件与分页 ==")
    sandbox = fresh_sandbox()
    cid = uuid.uuid4().hex

    m1 = await uistore.append_message(sandbox, cid, {"role": "user", "text": "你好"})
    m2 = await uistore.append_message(
        sandbox, cid, {"role": "assistant", "text": "回复", "usage": {"output": 3, "total": 9}}
    )
    path = sandbox / "ui_history" / f"{cid}.json"
    check("B1 每 cid 一文件", path.is_file(), str(path))
    check("B2 消息 id/ts 自动补全", m1["id"].startswith("m_") and "T" in m1["ts"])
    check("B3 无临时文件残留", not list(path.parent.glob("*.tmp")))
    doc = json.loads(path.read_text(encoding="utf-8"))
    check("B4 版本字段", doc.get("version") == uistore.SCHEMA_VERSION == 1)
    check("B5 messages 落盘 2 条", len(doc["messages"]) == 2)

    loaded = await uistore.load_conversation(sandbox, cid)
    check("B6 load 一致", [m["id"] for m in loaded["messages"]] == [m1["id"], m2["id"]])
    check("B7 has_user_message", await uistore.has_user_message(sandbox, cid) is True)
    check(
        "B8 空会话 has_user_message=False",
        await uistore.has_user_message(sandbox, uuid.uuid4().hex) is False,
    )

    for i in range(3):
        await uistore.append_message(sandbox, cid, {"role": "user", "text": f"msg{i}"})
    page1 = await uistore.page_messages(sandbox, cid, page=1, size=2)
    check("B9 page=1 为最新一页", [m["text"] for m in page1["messages"]] == ["msg1", "msg2"], str([m["text"] for m in page1["messages"]]))
    check("B10 total/size/page", (page1["total"], page1["size"], page1["page"]) == (5, 2, 1))
    check("B11 has_more=True", page1["has_more"] is True)
    check("B12 历史响应契约键", set(page1.keys()) == {"cid", "page", "size", "total", "has_more", "messages"})
    page3 = await uistore.page_messages(sandbox, cid, page=3, size=2)
    check("B13 最后一页含最早消息", [m["text"] for m in page3["messages"]] == ["你好"] and page3["has_more"] is False)
    page9 = await uistore.page_messages(sandbox, cid, page=9, size=2)
    check("B14 超页为空", page9["messages"] == [])

    # 迁移预留：未知版本尽力读取
    other = uuid.uuid4().hex
    legacy = sandbox / "ui_history" / f"{other}.json"
    legacy.write_text(json.dumps({"version": 0, "cid": other, "messages": [{"role": "user", "text": "hi"}]}, ensure_ascii=False), encoding="utf-8")
    migrated = await uistore.load_conversation(sandbox, other)
    check("B15 未知版本尽力读取", len(migrated["messages"]) == 1 and migrated["messages"][0]["text"] == "hi")

    # 损坏兜底：不清空用户数据
    broken = sandbox / "ui_history" / f"{uuid.uuid4().hex}.json"
    broken.write_text("not-json{{{", encoding="utf-8")
    doc_broken = await uistore.load_conversation(sandbox, broken.stem)
    check("B16 损坏兜底为空结构", doc_broken["messages"] == [])
    check("B17 损坏文件保留", broken.is_file() and broken.read_text(encoding="utf-8") == "not-json{{{")
    check("B18 缺失会话页为空", (await uistore.page_messages(sandbox, uuid.uuid4().hex))["total"] == 0)

    all_docs = await uistore.load_ui_history(sandbox)
    check("B19 全量扫描（损坏文件跳过）", set(all_docs["conversations"].keys()) >= {cid, other}, str(set(all_docs["conversations"].keys())))

    await uistore.clear_conversation(sandbox, cid)
    check("B20 clear 删除文件", not path.exists())


# ---------------------------------------------------------------------------
# C. conversation_service.py：umo / CRUD / 改题 / contexts
# ---------------------------------------------------------------------------


async def _test_conversation_service() -> None:
    print("== C. conversation_service umo 与会话 CRUD ==")
    check("C1 build_umo 形状", conversation_service.build_umo("abc") == "webui:FriendMessage:webui!abc")
    cid = uuid.uuid4().hex
    umo = conversation_service.build_umo(cid)
    check("C2 parse_umo 往返", conversation_service.parse_umo(umo) == cid)
    check("C3 外平台拒绝", conversation_service.parse_umo("aiocqhttp:GroupMessage:12345") is None)
    check("C4 非 FriendMessage 拒绝", conversation_service.parse_umo("webui:GroupMessage:webui!x") is None)
    check("C5 非 webui! 前缀拒绝", conversation_service.parse_umo("webui:FriendMessage:other!x") is None)
    check("C6 残缺 umo 拒绝", conversation_service.parse_umo("webui") is None and conversation_service.parse_umo("") is None)

    sandbox = fresh_sandbox()
    plugin = make_plugin(sandbox, provider=None)
    cm = plugin.context.conversation_manager

    info = await conversation_service.create_conversation(plugin.context, "p1", None)
    check("C7 新建返回 uuid4hex cid", len(info["cid"]) == 32 and all(ch in "0123456789abcdef" for ch in info["cid"]))
    check("C8 缺省标题 新会话", info["title"] == "新会话" and cm.convs[info["cid"]].title == "新会话")
    check("C9 平台命名空间落库", cm.convs[info["cid"]].platform_id == "webui")
    check("C10 persona 绑定", cm.convs[info["cid"]].persona_id == "p1")

    c_old = cm.seed("oldc", updated_at=100)
    c_new = cm.seed("newc", updated_at=200)
    cm.seed("foreign")  # user_id 属本插件命名空间
    cm.convs["foreign"].user_id = "aiocqhttp:GroupMessage:123"
    items = await conversation_service.list_conversations(plugin.context)
    cids = [item["cid"] for item in items]
    check("C11 updated_at 降序", cids.index("newc") < cids.index("oldc") if {"newc", "oldc"} <= set(cids) else False, str(cids))
    check("C12 外来会话被过滤", "foreign" not in cids)
    shape = items[0]
    check("C13 列表项契约键", set(shape.keys()) == {"cid", "title", "persona_id", "updated_at"})
    check("C14 updated_at ISO 带时区", "T" in shape["updated_at"] and ("+" in shape["updated_at"] or "Z" in shape["updated_at"]))

    await conversation_service.rename_conversation(plugin.context, c_new.cid, "这是一条超过二十个字的长标题应该被截断到二十字整")
    check("C15 update_conversation 改题截 20 字", cm.title_updates[-1] == (c_new.cid, "这是一条超过二十个字的长标题应该被截断到二十字整"[:20]))
    source = inspect.getsource(conversation_service)
    check("C16 未调用已废弃 update_conversation_title", "update_conversation_title(" not in source)

    await conversation_service.append_history_pair(plugin.context, c_new.cid, "用户说", "模型答")
    history = json.loads(cm.convs[c_new.cid].history)
    check("C17 权威历史双写", history == [{"role": "user", "content": "用户说"}, {"role": "assistant", "content": "模型答"}])
    check("C18 add_message_pair 调用记录", cm.pairs[-1][0] == c_new.cid)

    think_entry = {"role": "assistant", "content": [{"type": "think", "signature": "sig", "text": "思考"}]}
    history.append(think_entry)
    cm.convs[c_new.cid].history = json.dumps(history)
    contexts = await conversation_service.build_contexts(plugin.context, c_new.cid)
    check("C19 contexts 原样保留 ThinkPart/signature", contexts[-1] == think_entry)

    try:
        await conversation_service.build_contexts(plugin.context, "missing-cid")
        check("C20 会话不存在抛错", False)
    except conversation_service.ConversationNotFoundError:
        check("C20 会话不存在抛错", True)

    await conversation_service.delete_conversation(plugin.context, c_new.cid, sandbox)
    check("C21 级联删权威会话", c_new.cid in cm.deleted)
    check("C22 幂等删除", (await conversation_service.delete_conversation(plugin.context, c_new.cid, sandbox)) is None)


# ---------------------------------------------------------------------------
# D. chat_service.py：生成任务 / 事件序列 / 409 / abort / 双写
# ---------------------------------------------------------------------------


async def _test_chat_happy_path() -> None:
    print("== D1. 正常流式生成与双写 ==")
    chat_service._GENERATIONS.clear()
    sandbox = fresh_sandbox()
    text = "第一句话测试自动改题"
    provider = FakeProvider(
        [
            [_chunk("你"), _chunk(reasoning="思考一下"), _chunk("好"), _final("你好", "思考一下", {"input": 10, "output": 5})],
            [_chunk("再"), _final("再来", None, {"output": 2})],
        ]
    )
    plugin = make_plugin(sandbox, provider)
    info = await conversation_service.create_conversation(plugin.context, "p1", None)
    cid = info["cid"]

    gen_id = await chat_service.start_generation(plugin, cid, text)
    check("D1 gen_id uuid4hex", len(gen_id) == 32)
    check("D2 进行中可查", chat_service.active_generation_id(cid) == gen_id)
    entry = chat_service._GENERATIONS[gen_id]
    await await_task(entry["task"])

    events = await chat_service.replay_events(gen_id)
    check("D3 事件序列 text/reasoning/text/done", [e["event"] for e in events] == ["text_delta", "reasoning_delta", "text_delta", "done"], str([e["event"] for e in events]))
    check("D4 seq 从 0 递增", [e["data"]["seq"] for e in events] == [0, 1, 2, 3])
    check("D5 delta 内容", (events[0]["data"]["delta"], events[2]["data"]["delta"]) == ("你", "好"))
    done = events[3]["data"]
    check("D6 done 形状", done["message_id"].startswith("m_") and done["usage"] == {"output": 5, "total": 15}, str(done))
    check("D7 重放幂等（两次一致）", events == await chat_service.replay_events(gen_id))
    check("D8 终态后 active=None", chat_service.active_generation_id(cid) is None)
    check("D9 存活期内 generation_exists", chat_service.generation_exists(gen_id) is True)

    doc = await uistore.load_conversation(sandbox, cid)
    check("D10 双写-富历史两条", [m["role"] for m in doc["messages"]] == ["user", "assistant"])
    user_msg, assistant_msg = doc["messages"]
    check("D11 富历史文本一致", user_msg["text"] == text and assistant_msg["text"] == "你好")
    check("D12 reasoning 入富历史", assistant_msg.get("reasoning") == {"content": "思考一下"})
    check("D13 usage 入富历史", assistant_msg["usage"] == {"output": 5, "total": 15})
    check("D14 message_id 对齐", assistant_msg["id"] == done["message_id"])
    pair = plugin.context.conversation_manager.pairs[-1]
    check("D15 双写-权威历史一致", pair[1] == {"role": "user", "content": text} and pair[2] == {"role": "assistant", "content": "你好"})
    check("D16 首条消息自动改题", plugin.context.conversation_manager.title_updates == [(cid, text[:20])])

    gen2 = await chat_service.start_generation(plugin, cid, "第二句不重复改题")
    await await_task(chat_service._GENERATIONS[gen2]["task"])
    check("D17 仅首条消息改题", len(plugin.context.conversation_manager.title_updates) == 1)
    check("D18 provider 调用参数", provider.calls[0]["image_urls"] is None and provider.calls[0]["system_prompt"] == "你是测试人格")

    unknown = uuid.uuid4().hex
    try:
        await chat_service.replay_events(unknown)
        check("D19 未知 gen_id 404（重放）", False)
    except chat_service.GenerationNotFoundError:
        check("D19 未知 gen_id 404（重放）", True)
    check("D20 未知 gen_id 404（stop 返回 False）", await chat_service.abort_generation(unknown) is False)
    check("D21 generation_exists False", chat_service.generation_exists(unknown) is False)
    check("D22 text_chat_stream 仅 chat_service 触碰", len(provider.calls) == 2)


async def _test_chat_session_busy() -> None:
    print("== D2. 同会话 single-flight 409 ==")
    chat_service._GENERATIONS.clear()
    sandbox = fresh_sandbox()
    provider = FakeProvider([[_chunk("慢"), asyncio.Event()]])
    provider.gate = asyncio.Event()  # 永不置位：生成任务挂起在闸门（模拟进行中）
    plugin = make_plugin(sandbox, provider)
    info = await conversation_service.create_conversation(plugin.context, "p1", None)
    cid = info["cid"]

    gen_id = await chat_service.start_generation(plugin, cid, "第一条")
    entry = chat_service._GENERATIONS[gen_id]
    ok = await wait_for(lambda: any(e["event"] == "text_delta" for e in entry["buffer"]))
    check("D2-1 首个事件已产出", ok)

    try:
        await chat_service.start_generation(plugin, cid, "第二条")
        check("D2-2 同会话 409 session_busy", False)
    except chat_service.SessionBusyError as exc:
        check("D2-2 同会话 409 session_busy", exc.status_code == 409 and exc.slug == "session_busy")

    check("D2-3 abort 成功", await chat_service.abort_generation(gen_id) is True)
    await asyncio.wait([entry["task"]])
    check("D2-4 中止后可再发送（active=None）", chat_service.active_generation_id(cid) is None)


async def _test_chat_concurrency() -> None:
    print("== D3. 全局并发上限实时读取配置 ==")
    chat_service._GENERATIONS.clear()
    sandbox = fresh_sandbox()
    gate = asyncio.Event()
    provider = FakeProvider([[_chunk("a"), gate], [_chunk("b"), gate], [_chunk("c"), gate]])
    provider.gate = gate
    plugin = make_plugin(sandbox, provider, config={"max_concurrent_generations": 1})
    cm = plugin.context.conversation_manager
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

    gate.set()
    await asyncio.gather(
        await_task(chat_service._GENERATIONS[g1]["task"]),
        await_task(chat_service._GENERATIONS[g2]["task"]),
    )


async def _test_chat_abort() -> None:
    print("== D4. abort 先落 stopped 终态 + 重订阅 ==")
    chat_service._GENERATIONS.clear()
    sandbox = fresh_sandbox()
    text = "测试中止的部分回复"
    provider = FakeProvider([[_chunk("部分"), asyncio.Event()]])
    provider.gate = asyncio.Event()  # 永不置位：生成任务挂起在闸门（模拟进行中）
    plugin = make_plugin(sandbox, provider)
    cid = (await conversation_service.create_conversation(plugin.context, "p1", None))["cid"]

    gen_id = await chat_service.start_generation(plugin, cid, text)
    entry = chat_service._GENERATIONS[gen_id]
    ok = await wait_for(lambda: any(e["event"] == "text_delta" for e in entry["buffer"]))
    check("D4-1 首个增量已缓冲", ok)

    check("D4-2 stop 返回 True", await chat_service.abort_generation(gen_id) is True)
    await asyncio.wait([entry["task"]])
    check("D4-3 任务已取消", entry["task"].cancelled() or entry["task"].done())

    events = await chat_service.replay_events(gen_id)
    check("D4-4 终态为 stopped", events[-1]["event"] == "stopped", str([e["event"] for e in events]))
    check("D4-5 stopped 文案契约", events[-1]["data"] == {"seq": events[-1]["data"]["seq"], "message": "已手动停止"}, str(events[-1]["data"]))
    check("D4-6 stopped 在 text_delta 之后", [e["event"] for e in events] == ["text_delta", "stopped"])

    check("D4-7 已结束再 stop -> False(404)", await chat_service.abort_generation(gen_id) is False)

    # 重订阅：iter_events 全量重放，最后一条仍是终态 stopped（流会关）
    received: list[dict] = []
    async for item in chat_service.iter_events(gen_id):
        received.append(item)
    check("D4-8 重订阅重放一致", received == events)
    check("D4-9 重订阅见 stopped 终态", received[-1]["event"] == "stopped")

    doc = await uistore.load_conversation(sandbox, cid)
    check("D4-10 中止保留部分回复", doc["messages"][-1].get("text") == "部分" and doc["messages"][-1].get("stopped") is True)
    pair = plugin.context.conversation_manager.pairs[-1]
    check("D4-11 权威历史写入部分回复", pair[2] == {"role": "assistant", "content": "部分"})


async def _test_chat_provider_error() -> None:
    print("== D5. provider 异常 -> error 终态 ==")
    chat_service._GENERATIONS.clear()
    sandbox = fresh_sandbox()

    class _BoomProvider:
        def __init__(self) -> None:
            self.calls = 0

        def text_chat_stream(self, **kwargs):
            self.calls += 1
            return self._gen()

        async def _gen(self):
            yield _chunk("开头")
            raise RuntimeError("boom 上游炸了")

    provider = _BoomProvider()
    plugin = make_plugin(sandbox, provider)  # type: ignore[arg-type]
    cid = (await conversation_service.create_conversation(plugin.context, "p1", None))["cid"]

    gen_id = await chat_service.start_generation(plugin, cid, "触发异常")
    await await_task(chat_service._GENERATIONS[gen_id]["task"])
    events = await chat_service.replay_events(gen_id)
    check("D5-1 事件序列 delta+error", [e["event"] for e in events] == ["text_delta", "error"], str([e["event"] for e in events]))
    err = events[-1]["data"]
    check("D5-2 error fatal 终态", err.get("fatal") is True and err.get("code") == "server_error" and "boom" in err.get("message", ""), str(err))
    doc = await uistore.load_conversation(sandbox, cid)
    check("D5-3 失败占位消息", doc["messages"][-1].get("error") is not None and doc["messages"][-1]["role"] == "assistant")
    check("D5-4 失败不写权威历史", plugin.context.conversation_manager.pairs == [])


async def _test_chat_no_provider() -> None:
    print("== D6. 未配置 provider -> error 终态 ==")
    chat_service._GENERATIONS.clear()
    sandbox = fresh_sandbox()
    plugin = make_plugin(sandbox, provider=None)
    cid = (await conversation_service.create_conversation(plugin.context, None, None))["cid"]
    gen_id = await chat_service.start_generation(plugin, cid, "没有模型")
    await await_task(chat_service._GENERATIONS[gen_id]["task"])
    events = await chat_service.replay_events(gen_id)
    check("D6-1 error 终态", events[-1]["event"] == "error" and events[-1]["data"]["fatal"] is True)


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
        "H1-5 resolve_persona_tools 语义",
        persona_service.resolve_persona_tools(None) == (True, None)
        and persona_service.resolve_persona_tools([]) == (False, set())
        and persona_service.resolve_persona_tools(["a", "b"]) == (True, {"a", "b"}),
    )
    check(
        "H1-6 附件区块格式",
        attachment_service.build_attachment_block("a.md", "内容")
        == '<attachment name="a.md">\n内容\n</attachment>',
    )


async def _test_attachment_store() -> None:
    print("== H2. store_upload / 索引 / 截断 ==")
    sandbox = fresh_sandbox()
    config = {"attachment_text_whitelist": ["txt", "md"], "upload_image_max_mb": 1, "upload_text_max_kb": 200}

    text_upload = FakeUpload("notes.md", "text/markdown", "# 标题\n正文内容".encode("utf-8"))
    meta_text = await attachment_service.store_upload(text_upload, sandbox, config)
    check("H2-1 文本元数据契约键", set(meta_text.keys()) == {"file_id", "kind", "name", "size", "mime", "preview", "truncated"}, str(meta_text.keys()))
    check("H2-2 file_id 前缀", meta_text["file_id"].startswith("f_") and len(meta_text["file_id"]) == 34)
    check("H2-3 truncated False", meta_text["truncated"] is False)
    check("H2-4 preview 前 500 字符", meta_text["preview"].startswith("# 标题"))
    check("H2-5 mime text/markdown", meta_text["mime"] == "text/markdown")
    check("H2-6 落盘 uploads/<yyyyMM>/", text_upload.saved_to is not None and text_upload.saved_to.parent.name == datetime.now(timezone.utc).strftime("%Y%m") and text_upload.saved_to.name.startswith(meta_text["file_id"]))
    check("H2-7 磁盘名同源 file_id", f"{meta_text['file_id']}_" in text_upload.saved_to.name)

    meta = await attachment_service.get_file(sandbox, meta_text["file_id"])
    check(
        "H2-8 索引登记（含内部字段）",
        meta is not None and meta["cid"] is None and meta["path"].startswith("uploads/"),
        str(meta),
    )
    check("H2-9 public_meta 剥离内部字段", "path" not in attachment_service.public_meta(meta) and "cid" not in attachment_service.public_meta(meta))

    png = FakeUpload("pic.png", "image/png", b"\x89PNG\r\n\x1a\nfake")
    meta_img = await attachment_service.store_upload(png, sandbox, config)
    check("H2-10 图片 kind/mime", meta_img["kind"] == "image" and meta_img["mime"] == "image/png")
    check("H2-11 图片无 truncated 键", "truncated" not in meta_img)
    check("H2-12 图片 preview 缺省或 data URL（无 PIL 环境降级，真机为 data URL）", "preview" not in meta_img or meta_img["preview"] is None or str(meta_img["preview"]).startswith("data:image/"))
    check("H2-13 图片按 max_bytes 落盘", png.save_max_bytes == 1 * 1024 * 1024)

    # 文本截断：300KB > 200KB -> truncated=True；磁盘保留全文
    big = ("甲" * 200).encode("utf-8") * 1600  # 200 字符 * 3字节 * 1600 ≈ 960KB
    big_upload = FakeUpload("big.txt", "text/plain", big)
    meta_big = await attachment_service.store_upload(big_upload, sandbox, config)
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
        await attachment_service.store_upload(over, sandbox, config)
        check("H2-18 图片超限 413 前置", False)
    except attachment_service.UploadTooLargeError:
        check("H2-18 图片超限 413 前置", True)

    # 白名单外 -> UnsupportedMediaTypeError（routes -> 415）
    exe = FakeUpload("virus.exe", "application/x-msdownload", b"MZ")
    try:
        await attachment_service.store_upload(exe, sandbox, config)
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
    old_orphan = await attachment_service.store_upload(FakeUpload("old.txt", "text/plain", b"old"), sandbox, config)
    new_orphan = await attachment_service.store_upload(FakeUpload("new.txt", "text/plain", b"new"), sandbox, config)
    bound_old = await attachment_service.store_upload(FakeUpload("bound.txt", "text/plain", b"bound"), sandbox, config)
    await attachment_service.bind_file(sandbox, bound_old["file_id"], "c1")
    # 把 old_orphan 的登记时间改为 25h 前（sweep 可注入 now，不改全局时钟）
    index = await attachment_service._load_index(sandbox)
    index["files"][old_orphan["file_id"]]["created_at"] = (now - timedelta(hours=25)).isoformat()
    await attachment_service._save_index(sandbox, index)
    old_disk = sandbox / index["files"][old_orphan["file_id"]]["path"]

    removed = await attachment_service.sweep_orphans(sandbox, now=now)
    check("H3-1 仅清理超 24h 未引用孤儿", removed == 1)
    check("H3-2 孤儿磁盘文件删除", not old_disk.exists() and await attachment_service.get_file(sandbox, old_orphan["file_id"]) is None)
    check("H3-3 新孤儿保留", await attachment_service.get_file(sandbox, new_orphan["file_id"]) is not None)
    check("H3-4 已引用不清理", await attachment_service.get_file(sandbox, bound_old["file_id"]) is not None)

    # 后台任务生命周期：启动 -> 运行中 -> 取消
    plugin = make_plugin(sandbox, provider=None)
    await attachment_service.start_orphan_cleanup(plugin)
    task = attachment_service._CLEANUP_TASK
    check("H3-5 清理任务已启动", task is not None and not task.done())
    await attachment_service.start_orphan_cleanup(plugin)  # 幂等
    check("H3-6 幂等启动", attachment_service._CLEANUP_TASK is task)
    await attachment_service.stop_orphan_cleanup()
    check("H3-7 清理任务已取消", task.done())
    await attachment_service.stop_orphan_cleanup()  # 幂等
    check("H3-8 幂等停止", attachment_service._CLEANUP_TASK is None)


# ---------------------------------------------------------------------------
# I. tool_loop.py：ToolSet 过滤 / 工具执行 / 调用解析 / 回填构造
# ---------------------------------------------------------------------------


def test_tool_loop_unit() -> None:
    print("== I1. tool_loop 纯函数 ==")
    calls_final = SimpleNamespace(
        tools_call_name=["a", "b"],
        tools_call_args=[{"x": 1}, '{"y": 2}'],
        tools_call_ids=["call_0", "call_1"],
    )
    calls = tool_loop.extract_tool_calls(calls_final)
    check("I1-1 工具调用解析", calls == [{"id": "call_0", "name": "a", "args": {"x": 1}}, {"id": "call_1", "name": "b", "args": {"y": 2}}], str(calls))
    check("I1-2 空响应解析", tool_loop.extract_tool_calls(None) == [])
    check("I1-3 缺 ids 兜底编号", tool_loop.extract_tool_calls(SimpleNamespace(tools_call_name=["a"]))[0]["id"] == "call_0")

    result = tool_loop.build_tool_calls_result(["c1"], ["get_weather"], ['{"location": "北京"}'], ["晴"], "done")
    check(
        "I1-4 ToolCallsResult 主候选字段构造",
        getattr(result, "func_name", None) == ["get_weather"]
        and getattr(result, "func_args", None) == ['{"location": "北京"}']
        and getattr(result, "func_result", None) == ["晴"]
        and getattr(result, "completion_text", None) == "done",
    )

    ok, err = asyncio.run(tool_loop.execute_tool(FakeTool("t", result="结果"), object(), "t", {"a": 1}))
    check("I1-5 工具执行成功", ok == "结果" and err is None)
    ok, err = asyncio.run(tool_loop.execute_tool(FakeTool("t", error="炸了"), object(), "t", {}))
    check("I1-6 工具执行失败转 error 文本", ok is None and "炸了" in (err or ""))
    ok, err = asyncio.run(tool_loop.execute_tool(object(), object(), "t", {}))
    check("I1-7 无 call 接口转 error 文本", ok is None and err is not None)


async def _test_tool_set_filter() -> None:
    print("== I2. build_toolset 人格 tools 语义 ==")
    sandbox = fresh_sandbox()
    plugin = make_plugin(sandbox, provider=None)
    tool_a, tool_b = FakeTool("get_weather"), FakeTool("search")
    plugin.context.tool_manager = FakeToolManager([tool_a, tool_b])

    toolset, objs = tool_loop.build_toolset(plugin, {"tools": None})
    check("I2-1 None=全部工具", toolset is not None and set(objs) == {"get_weather", "search"})
    toolset, objs = tool_loop.build_toolset(plugin, {"tools": []})
    check("I2-2 空列表=禁用", toolset is None and objs == {})
    toolset, objs = tool_loop.build_toolset(plugin, {"tools": ["search"]})
    check("I2-3 列表=白名单", toolset is not None and set(objs) == {"search"})
    plugin.context.tool_manager = None
    toolset, objs = tool_loop.build_toolset(plugin, {"tools": None})
    check("I2-4 无工具管理器 -> 无工具", toolset is None and objs == {})


# ---------------------------------------------------------------------------
# J. chat_service 集成：工具循环 / 富历史 tools / 多模态错误 / 附件链路
# ---------------------------------------------------------------------------


async def _test_chat_tool_loop() -> None:
    print("== J1. 方案 A 流式工具循环端到端 ==")
    chat_service._GENERATIONS.clear()
    sandbox = fresh_sandbox()
    round1_final = SimpleNamespace(
        is_chunk=False,
        result_chain=FakeChain(""),
        completion_text="",
        reasoning_content=None,
        reasoning_signature=None,
        usage=None,
        tools_call_name=["get_weather"],
        tools_call_args=[{"location": "北京"}],
        tools_call_ids=["call_1"],
    )
    provider = FakeProvider(
        [
            [round1_final],
            [_chunk("今天"), _final("今天北京晴", None, {"input": 3, "output": 7})],
        ]
    )
    plugin = make_plugin(sandbox, provider)
    tool = FakeTool("get_weather", result="晴 26℃")
    plugin.context.tool_manager = FakeToolManager([tool])
    cid = (await conversation_service.create_conversation(plugin.context, "p1", None))["cid"]

    gen_id = await chat_service.start_generation(plugin, cid, "北京天气怎么样")
    entry = chat_service._GENERATIONS[gen_id]
    await await_task(entry["task"])

    events = await chat_service.replay_events(gen_id)
    types = [e["event"] for e in events]
    check("J1-1 事件序列 tool_call/tool_result/text_delta/done", types == ["tool_call", "tool_result", "text_delta", "done"], str(types))
    check("J1-2 tool_call 事件契约", events[0]["data"] == {"seq": 0, "phase": "start", "id": "call_1", "name": "get_weather", "args": {"location": "北京"}}, str(events[0]["data"]))
    tr = events[1]["data"]
    check("J1-3 tool_result 事件契约", tr.get("id") == "call_1" and tr.get("result") == "晴 26℃" and isinstance(tr.get("elapsed_ms"), int) and "error" not in tr, str(tr))
    check("J1-4 工具真实执行", tool.calls == [{"location": "北京"}])
    check("J1-5 done usage", events[3]["data"]["usage"] == {"output": 7, "total": 10})
    check("J1-6 第二轮 func_tool/tool_calls_result", provider.calls[0].get("func_tool") is not None and provider.calls[1].get("func_tool") is not None and provider.calls[1].get("tool_calls_result") is not None)
    tc_result = provider.calls[1]["tool_calls_result"]
    check("J1-7 回填内容", list(getattr(tc_result, "func_name", [])) == ["get_weather"] and "晴 26℃" in list(getattr(tc_result, "func_result", [""]))[0])

    doc = await uistore.load_conversation(sandbox, cid)
    assistant = doc["messages"][-1]
    check("J1-8 富历史含工具步骤", assistant["role"] == "assistant" and len(assistant["tools"]) == 1 and assistant["tools"][0]["name"] == "get_weather" and assistant["tools"][0]["result"] == "晴 26℃" and "elapsed_ms" in assistant["tools"][0], str(assistant.get("tools")))
    pair = plugin.context.conversation_manager.pairs[-1]
    check("J1-9 权威历史落最终文本", pair[2] == {"role": "assistant", "content": "今天北京晴"})


async def _test_chat_tool_loop_agent() -> None:
    print("== J2. 方案 B tool_loop_agent 模式 ==")
    chat_service._GENERATIONS.clear()
    sandbox = fresh_sandbox()
    provider = FakeProvider([[_chunk("x"), _final("x", None, None)]])  # 不应被消费
    plugin = make_plugin(sandbox, provider, config={"tool_loop_mode": "loop_agent"})
    plugin.context.tool_manager = FakeToolManager([FakeTool("get_weather")])
    cid = (await conversation_service.create_conversation(plugin.context, "p1", None))["cid"]

    gen_id = await chat_service.start_generation(plugin, cid, "北京天气")
    entry = chat_service._GENERATIONS[gen_id]
    await await_task(entry["task"])

    events = await chat_service.replay_events(gen_id)
    types = [e["event"] for e in events]
    check("J2-1 方案 B 走宿主循环", plugin.context.agent_calls and provider.calls == [], f"provider.calls={provider.calls}")
    check("J2-2 钩子产生工具事件", [t for t in types if t in ("tool_call", "tool_result")] == ["tool_call", "tool_result"], str(types))
    check("J2-3 done 正文来自宿主响应", events[-1]["event"] == "done" and events[-1]["data"]["usage"] == {"output": 4, "total": 6})
    doc = await uistore.load_conversation(sandbox, cid)
    check("J2-4 富历史正文", doc["messages"][-1]["text"] == "宿主循环答案" and doc["messages"][-1]["tools"][0]["result"] == "晴 26℃")


async def _test_chat_tool_degrade() -> None:
    print("== J3. 方案 A -> 方案 B -> 无工具 降级阶梯 ==")
    chat_service._GENERATIONS.clear()
    sandbox = fresh_sandbox()
    provider = FakeProvider([[_chunk("直接答案"), _final("直接答案", None, {"output": 2})]])
    plugin = make_plugin(sandbox, provider)
    plugin.context.tool_manager = FakeToolManager([FakeTool("get_weather")])
    cid = (await conversation_service.create_conversation(plugin.context, "p1", None))["cid"]

    real_build = tool_loop.build_exec_context
    try:
        tool_loop.build_exec_context = lambda plugin_: None  # 令方案 A 不可用
        gen_id = await chat_service.start_generation(plugin, cid, "带工具提问")
        entry = chat_service._GENERATIONS[gen_id]
        await await_task(entry["task"])
    finally:
        tool_loop.build_exec_context = real_build

    events = await chat_service.replay_events(gen_id)
    types = [e["event"] for e in events]
    nonfatal = [e["data"] for e in events if e["event"] == "error"]
    check("J3-1 降级提示非致命", any(e.get("fatal") is False and "改用宿主逐步执行" in e.get("message", "") for e in nonfatal), str(nonfatal))
    check("J3-2 降级走方案 B", bool(plugin.context.agent_calls))
    check("J3-3 最终仍 done", types[-1] == "done" and plugin.context.conversation_manager.pairs[-1][2]["content"] == "宿主循环答案")


async def _test_chat_multimodal_error() -> None:
    print("== J4. 图片被拒 -> error{code:model_not_multimodal} ==")
    chat_service._GENERATIONS.clear()
    sandbox = fresh_sandbox()

    class _NoImageProvider:
        def text_chat_stream(self, **kwargs):
            return self._gen()

        async def _gen(self):
            raise RuntimeError("model does not support image input")
            yield  # pragma: no cover

    plugin = make_plugin(sandbox, _NoImageProvider())  # type: ignore[arg-type]
    cid = (await conversation_service.create_conversation(plugin.context, "p1", None))["cid"]
    gen_id = await chat_service.start_generation(plugin, cid, "看这张图", image_urls=["/tmp/a.png"])
    await await_task(chat_service._GENERATIONS[gen_id]["task"])
    events = await chat_service.replay_events(gen_id)
    err = events[-1]["data"]
    check("J4-1 error code=model_not_multimodal", events[-1]["event"] == "error" and err.get("fatal") is True and err.get("code") == "model_not_multimodal", str(err))

    # 无图片的普通错误仍为 server_error（D5 已覆盖）；此处验证图片错误启发式不含关键词时回落
    chat_service._GENERATIONS.clear()

    class _OtherProvider:
        def text_chat_stream(self, **kwargs):
            return self._gen()

        async def _gen(self):
            raise RuntimeError("quota exceeded")
            yield  # pragma: no cover

    plugin2 = make_plugin(sandbox, _OtherProvider())  # type: ignore[arg-type]
    cid2 = (await conversation_service.create_conversation(plugin2.context, "p1", None))["cid"]
    gen2 = await chat_service.start_generation(plugin2, cid2, "看这张图", image_urls=["/tmp/a.png"])
    await await_task(chat_service._GENERATIONS[gen2]["task"])
    err2 = (await chat_service.replay_events(gen2))[-1]["data"]
    check("J4-2 非多模态错误回落 server_error", err2.get("code") == "server_error")


async def _test_chat_text_attachment() -> None:
    print("== J5. 文本附件注入 prompt ==")
    chat_service._GENERATIONS.clear()
    sandbox = fresh_sandbox()
    provider = FakeProvider([[_final("收到", None, None)]])
    plugin = make_plugin(sandbox, provider)
    cid = (await conversation_service.create_conversation(plugin.context, "p1", None))["cid"]
    block = attachment_service.build_attachment_block("a.md", "文件内容")
    meta = [{"file_id": "f_x", "kind": "text", "name": "a.md", "size": 12, "mime": "text/markdown", "preview": "文件内容", "truncated": False}]
    gen_id = await chat_service.start_generation(plugin, cid, "总结附件", attachment_blocks=[block], attachment_meta=meta)
    await await_task(chat_service._GENERATIONS[gen_id]["task"])
    check("J5-1 prompt 拼附件区块", provider.calls[0]["prompt"] == f"总结附件\n\n{block}")
    doc = await uistore.load_conversation(sandbox, cid)
    check("J5-2 user 消息 attachments 同构", doc["messages"][0]["attachments"] == meta)
    # 仅附件无文本：prompt 为附件区块本身
    chat_service._GENERATIONS.clear()
    provider2 = FakeProvider([[_final("收到", None, None)]])
    plugin2 = make_plugin(sandbox, provider2)
    cid2 = (await conversation_service.create_conversation(plugin2.context, "p1", None))["cid"]
    gen2 = await chat_service.start_generation(plugin2, cid2, "", attachment_blocks=[block], attachment_meta=meta)
    await await_task(chat_service._GENERATIONS[gen2]["task"])
    check("J5-3 纯附件 prompt", provider2.calls[0]["prompt"] == block)


async def _test_chat_concurrent_send() -> None:
    print("== D7. 并发 send 竞态（TOCTOU 修复回归）==")
    # 同 cid 并发：占位登记与检查间无 await，第二条必须拿到 409
    chat_service._GENERATIONS.clear()
    sandbox = fresh_sandbox()
    provider = FakeProvider([[_chunk("一"), asyncio.Event()], [_chunk("二"), asyncio.Event()]])
    provider.gate = asyncio.Event()
    plugin = make_plugin(sandbox, provider)
    cid = (await conversation_service.create_conversation(plugin.context, "p1", None))["cid"]

    results = await asyncio.gather(
        chat_service.start_generation(plugin, cid, "并发甲"),
        chat_service.start_generation(plugin, cid, "并发乙"),
        return_exceptions=True,
    )
    ok = [r for r in results if isinstance(r, str)]
    busy = [r for r in results if isinstance(r, chat_service.SessionBusyError)]
    check("D7-1 同 cid 并发恰一条放行", len(ok) == 1 and len(busy) == 1, f"ok={ok} busy={len(busy)} other={[r for r in results if not isinstance(r, (str, chat_service.SessionBusyError))]}")
    check("D7-2 放行者持 single-flight 锁", chat_service.active_generation_id(cid) == ok[0])
    doc_d7 = await uistore.load_conversation(sandbox, cid)
    check("D7-3 被拒者未写用户消息（仅 1 条 user）", len([m for m in doc_d7["messages"] if m["role"] == "user"]) == 1, str(len(doc_d7["messages"])))

    # 不同 cid 并发 + limit=1：并发上限同样不再被穿透
    chat_service._GENERATIONS.clear()
    plugin2 = make_plugin(sandbox, provider, config={"max_concurrent_generations": 1})
    c1 = (await conversation_service.create_conversation(plugin2.context, None, None))["cid"]
    c2 = (await conversation_service.create_conversation(plugin2.context, None, None))["cid"]
    results2 = await asyncio.gather(
        chat_service.start_generation(plugin2, c1, "并发一"),
        chat_service.start_generation(plugin2, c2, "并发二"),
        return_exceptions=True,
    )
    ok2 = [r for r in results2 if isinstance(r, str)]
    limit = [r for r in results2 if isinstance(r, chat_service.ConcurrencyLimitError)]
    check("D7-4 limit=1 并发恰一条放行", len(ok2) == 1 and len(limit) == 1, f"ok={len(ok2)} limit={len(limit)}")
    check("D7-5 活跃任务数不超过上限", chat_service._active_count() == 1)
    provider.gate.set()  # 收尾
    for gen_id, entry in list(chat_service._GENERATIONS.items()):
        await await_task(entry["task"])
    check("D7-6 全部收尾后活跃归零", chat_service._active_count() == 0)


async def _test_delete_while_generating() -> None:
    print("== D8. 删除生成中的会话（中止收尾先行，富历史不复活）==")
    chat_service._GENERATIONS.clear()
    sandbox = fresh_sandbox()
    provider = FakeProvider([[_chunk("开头"), asyncio.Event()]])
    provider.gate = asyncio.Event()  # 挂起：模拟生成中
    plugin = make_plugin(sandbox, provider)
    cid = (await conversation_service.create_conversation(plugin.context, "p1", None))["cid"]
    gen_id = await chat_service.start_generation(plugin, cid, "会被中止的消息")
    entry = chat_service._GENERATIONS[gen_id]
    await wait_for(lambda: any(e["event"] == "text_delta" for e in entry["buffer"]))

    # abort(wait=True) 等中止分支（partial 富历史/权威历史写入）完成后，删除才执行
    check("D8-1 abort(wait=True) 成功", await chat_service.abort_generation(gen_id, wait=True) is True)
    check("D8-2 任务已彻底收尾", entry["task"].done())
    partial_doc = await uistore.load_conversation(sandbox, cid)
    check("D8-3 中止部分回复已落盘（删除前）", partial_doc["messages"][-1].get("text") == "开头")

    await conversation_service.delete_conversation(plugin.context, cid, sandbox)
    history_file = sandbox / "ui_history" / f"{cid}.json"
    check("D8-4 删除后富历史不复活", not history_file.exists())
    check("D8-5 删除后无活跃任务", chat_service.active_generation_id(cid) is None)
    await await_task(entry["task"])  # 双保险收尾（wait=True 已完成，此处幂等）


async def _test_sse_ping() -> None:
    print("== J6. SSE 保活 ping（空闲超时发帧，不杀底层流）==")
    chat_service._GENERATIONS.clear()
    sandbox = fresh_sandbox()
    provider = FakeProvider([[_chunk("慢"), asyncio.Event()]])
    provider.gate = asyncio.Event()  # 永不置位：生成挂起，流空闲
    plugin = make_plugin(sandbox, provider)
    cid = (await conversation_service.create_conversation(plugin.context, "p1", None))["cid"]
    gen_id = await chat_service.start_generation(plugin, cid, "挂着的生成")
    entry = chat_service._GENERATIONS[gen_id]
    await wait_for(lambda: len(entry["buffer"]) > 0)

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
        check("J6-4 业务帧仍在 ping 之前重放", frames[0].startswith("event: text_delta"), frames[0])
    finally:
        routes.SSE_PING_INTERVAL_SECONDS = real_interval
        await chat_service.abort_generation(gen_id, wait=True)


async def _collect_frames(source, frames: list[str], limit: int) -> None:
    """消费 SSE 帧生成器直到取满 limit 帧（测试辅助）。"""
    async for frame in source:
        frames.append(frame)
        if len(frames) >= limit:
            return


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


# ---------------------------------------------------------------------------
# E. routes.py：精确路由注册 / 错误包 / 端点行为
# ---------------------------------------------------------------------------


def test_routes() -> None:
    print("== E. routes 路由表与错误包 ==")
    sandbox = fresh_sandbox()
    chat_service._GENERATIONS.clear()
    plugin = make_plugin(sandbox, provider=None)
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

        # conversations: list + busy
        stub_request.set_json(None)
        resp = await call("/astrbot_plugin_webui/conversations", "GET")
        items = resp["data"]["conversations"]
        check("E13 会话列表 busy 键", all(set(i.keys()) == {"cid", "title", "persona_id", "updated_at", "busy"} for i in items))
        chat_service._GENERATIONS["fake-gen"] = {"gen_id": "fake-gen", "cid": cid, "task": None, "buffer": sse.new_buffer(), "waiters": set(), "finished": False, "finished_at": None, "last_seq": -1}
        resp = await call("/astrbot_plugin_webui/conversations", "GET")
        busy_map = {i["cid"]: i["busy"] for i in resp["data"]["conversations"]}
        check("E14 busy=该 cid 有进行中任务", busy_map.get(cid) is True)
        chat_service._GENERATIONS.clear()

        # history
        await uistore.append_message(plugin.data_dir(), cid, {"role": "user", "text": "历史消息"})
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
        check("E20 M3 前 file_ids 明确 400", resp["status"] == 400)
        stub_request.set_json({"cid": "missing", "text": "hi"})
        resp = await call("/astrbot_plugin_webui/send", "POST")
        check("E21 未知 cid 404", resp["status"] == 404 and resp["data"]["error"] == "not_found")

        # send 正常 + session_busy
        gate = asyncio.Event()
        provider = FakeProvider([[_chunk("慢"), gate], [_chunk("慢"), gate]])
        provider.gate = gate  # 挂起在闸门：模拟进行中
        plugin.context.provider = provider
        stub_request.set_json({"cid": cid, "text": "你好"})
        resp = await call("/astrbot_plugin_webui/send", "POST")
        gen_id = resp["data"].get("gen_id", "")
        check("E22 send 恒 200 {gen_id}", resp["status"] == 200 and len(gen_id) == 32)
        await wait_for(lambda: len(chat_service._GENERATIONS.get(gen_id, {}).get("buffer", [])) > 0)
        stub_request.set_json({"cid": cid, "text": "第二条"})
        resp = await call("/astrbot_plugin_webui/send", "POST")
        check("E23 session_busy 409", resp["status"] == 409 and resp["data"]["error"] == "session_busy" and resp["data"]["code"] == 409)

        # stop / stream
        stub_request.set_json(None)
        resp = await call("/astrbot_plugin_webui/stop/<gen_id>", "POST", gen_id=gen_id)
        check("E24 stop -> ok/stopped", resp["status"] == 200 and resp["data"] == {"ok": True, "stopped": True})
        await asyncio.wait([chat_service._GENERATIONS[gen_id]["task"]])
        resp = await call("/astrbot_plugin_webui/stop/<gen_id>", "POST", gen_id=gen_id)
        check("E25 已结束 stop 404", resp["status"] == 404)

        stream_resp = await call("/astrbot_plugin_webui/stream/<gen_id>", "GET", gen_id=gen_id)
        check("E26 stream 200 + event-stream", stream_resp["__type"] == "stream" and stream_resp["status"] == 200 and stream_resp["content_type"] == "text/event-stream", str(stream_resp))

        # stream 内容重放（消费 SSE 生成器）
        frames: list[str] = []
        async for frame in stream_resp["content"]:
            frames.append(frame)
        check("E28 SSE 帧含终态", any("event: stopped" in f for f in frames))
        check("E29 SSE data 含 seq", all('"seq": ' in f for f in frames))

        resp = await call("/astrbot_plugin_webui/stream/<gen_id>", "GET", gen_id="missing")
        check("E27 未知 gen stream 404", resp["status"] == 404)

        # delete（POST 别名）幂等 + 级联
        stub_request.set_json(None)
        resp = await call("/astrbot_plugin_webui/conversations/<cid>/delete", "POST", cid=cid)
        check("E30 delete 别名 ok", resp["status"] == 200 and resp["data"] == {"ok": True})
        resp2 = await call("/astrbot_plugin_webui/conversations/<cid>/delete", "POST", cid=cid)
        check("E31 delete 幂等", resp2["status"] == 200)
        check("E32 级联删权威会话", cid in plugin.context.conversation_manager.deleted)
        check("E33 级联删富历史", not (plugin.data_dir() / "ui_history" / f"{cid}.json").exists())

        resp = await call("/astrbot_plugin_webui/conversations/<cid>", "DELETE", cid=cid)
        check("E34 DELETE 等价", resp["status"] == 200 and resp["data"] == {"ok": True})

        gate.set()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# K. routes：upload / files 端点 + send 附件链路 + 级联删除
# ---------------------------------------------------------------------------


async def _test_routes_attachments() -> None:
    print("== K. routes 附件端点与 send 附件链路 ==")
    chat_service._GENERATIONS.clear()
    sandbox = fresh_sandbox()
    config = {"attachment_text_whitelist": ["txt", "md"], "upload_image_max_mb": 1, "upload_text_max_kb": 200}
    plugin = make_plugin(sandbox, provider=None, config=config)

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

    # upload：图片（无 PIL 环境 preview 允许 None；真机为 data URL）
    png_upload = FakeUpload("b.png", "image/png", b"\x89PNG fake")
    stub_request.set_files({"file": png_upload})
    resp = await call("/astrbot_plugin_webui/upload", "POST")
    check("K-3 upload 图片元数据", resp["status"] == 200 and resp["data"]["kind"] == "image" and resp["data"]["mime"] == "image/png" and "truncated" not in resp["data"])
    fid_img = resp["data"]["file_id"]

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

    # send + file_ids 端到端
    provider = FakeProvider([[_final("看完了", None, {"output": 2, "total": 3})]])
    plugin.context.provider = provider
    cid = (await conversation_service.create_conversation(plugin.context, "p1", None))["cid"]
    stub_request.set_json({"cid": cid, "text": "看看这两个附件", "file_ids": [fid_text, fid_img]})
    resp = await call("/astrbot_plugin_webui/send", "POST")
    gen_id = resp["data"].get("gen_id", "")
    check("K-8 send 恒 200 {gen_id}", resp["status"] == 200 and len(gen_id) == 32)
    await await_task(chat_service._GENERATIONS[gen_id]["task"])

    check(
        "K-9 图片路径直入 image_urls",
        len(provider.calls[0]["image_urls"]) == 1
        and provider.calls[0]["image_urls"][0].endswith(f"{fid_img}_b.png")
        and str(sandbox) in provider.calls[0]["image_urls"][0],
    )
    check("K-10 文本附件拼区块", '<attachment name="a.md">' in provider.calls[0]["prompt"] and "看看这两个附件" in provider.calls[0]["prompt"])

    doc = await uistore.load_conversation(sandbox, cid)
    user_msg = doc["messages"][0]
    check("K-11 user 消息 attachments 元数据同构", [a["file_id"] for a in user_msg["attachments"]] == [fid_text, fid_img] and user_msg["attachments"][1]["kind"] == "image")
    bound_text = await attachment_service.get_file(sandbox, fid_text)
    bound_img = await attachment_service.get_file(sandbox, fid_img)
    check("K-12 索引引用绑定", bound_text["cid"] == cid and bound_img["cid"] == cid)

    # send：未知 file_id 400；仅附件无文本 200
    stub_request.set_json({"cid": cid, "text": "", "file_ids": ["f_missing"]})
    resp = await call("/astrbot_plugin_webui/send", "POST")
    check("K-13 未知 file_id 400", resp["status"] == 400)
    stub_request.set_json({"cid": cid, "text": "", "file_ids": [fid_text]})
    resp = await call("/astrbot_plugin_webui/send", "POST")
    check("K-14 纯附件发送 200", resp["status"] == 200 and len(resp["data"]["gen_id"]) == 32)
    await await_task(chat_service._GENERATIONS[resp["data"]["gen_id"]]["task"])
    check("K-15 纯附件 prompt 为区块", provider.calls[1]["prompt"].startswith('<attachment name="a.md">'))

    # 删除会话级联：附件文件 + 索引 + 富历史
    stub_request.set_json(None)
    resp = await call("/astrbot_plugin_webui/conversations/<cid>/delete", "POST", cid=cid)
    check("K-16 删除会话 ok", resp["status"] == 200 and resp["data"] == {"ok": True})
    check("K-17 附件级联删除", await attachment_service.get_file(sandbox, fid_text) is None and await attachment_service.get_file(sandbox, fid_img) is None)
    check("K-18 附件磁盘删除", not text_upload.saved_to.exists() and not png_upload.saved_to.exists())


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
        attachment_service, tool_loop, uistore, sse, standalone,
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
    asyncio.run(_test_chat_provider_error())
    asyncio.run(_test_chat_no_provider())
    test_attachment_unit()
    asyncio.run(_test_attachment_store())
    asyncio.run(_test_orphan_sweep())
    test_tool_loop_unit()
    asyncio.run(_test_tool_set_filter())
    asyncio.run(_test_chat_tool_loop())
    asyncio.run(_test_chat_tool_loop_agent())
    asyncio.run(_test_chat_tool_degrade())
    asyncio.run(_test_chat_multimodal_error())
    asyncio.run(_test_chat_text_attachment())
    asyncio.run(_test_chat_concurrent_send())
    asyncio.run(_test_delete_while_generating())
    asyncio.run(_test_sse_ping())
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
