"""stub ``astrbot.core.platform.sources.webchat.webchat_queue_mgr``：webchat 队列桩。

按宿主 webchat_queue_mgr.py 同名单例的最小形状复刻（get_or_create_queue /
get_or_create_back_queue / put_back_queue / remove_back_queue / remove_queues）；
**不实现 set_listener 消费循环**（桩宿主无 webchat 适配器）——测试可通过
``back_queues[gen_id]`` 手工投递载荷驱动排水翻译（第三步收敛用）。
"""

import asyncio


class WebChatQueueMgr:
    def __init__(self, queue_maxsize: int = 128, back_queue_maxsize: int = 512) -> None:
        self.queues: dict[str, asyncio.Queue] = {}
        self.back_queues: dict[str, asyncio.Queue] = {}
        self._back_queue_close_events: dict[str, asyncio.Event] = {}
        self._conversation_back_requests: dict[str, set[str]] = {}
        self._request_conversation: dict[str, str] = {}
        self._queue_close_events: dict[str, asyncio.Event] = {}
        self.queue_maxsize = queue_maxsize
        self.back_queue_maxsize = back_queue_maxsize

    def get_or_create_queue(self, conversation_id: str) -> asyncio.Queue:
        if conversation_id not in self.queues:
            self.queues[conversation_id] = asyncio.Queue(maxsize=self.queue_maxsize)
            self._queue_close_events[conversation_id] = asyncio.Event()
        return self.queues[conversation_id]

    def get_or_create_back_queue(self, request_id: str, conversation_id: str | None = None):
        if request_id not in self.back_queues:
            self.back_queues[request_id] = asyncio.Queue(maxsize=self.back_queue_maxsize)
            self._back_queue_close_events[request_id] = asyncio.Event()
        if conversation_id:
            self._request_conversation[request_id] = conversation_id
            self._conversation_back_requests.setdefault(conversation_id, set()).add(request_id)
        return self.back_queues[request_id]

    async def put_back_queue(self, request_id: str, data: object) -> bool:
        queue = self.back_queues.get(request_id)
        close_event = self._back_queue_close_events.get(request_id)
        if queue is None or close_event is None or close_event.is_set():
            return False
        try:
            queue.put_nowait(data)
            return True
        except asyncio.QueueFull:
            return False

    def remove_back_queue(self, request_id: str):
        close_event = self._back_queue_close_events.pop(request_id, None)
        if close_event is not None:
            close_event.set()
        self.back_queues.pop(request_id, None)
        conversation_id = self._request_conversation.pop(request_id, None)
        if conversation_id:
            request_ids = self._conversation_back_requests.get(conversation_id)
            if request_ids is not None:
                request_ids.discard(request_id)
                if not request_ids:
                    self._conversation_back_requests.pop(conversation_id, None)

    def remove_queues(self, conversation_id: str) -> None:
        for request_id in list(self._conversation_back_requests.get(conversation_id, set())):
            self.remove_back_queue(request_id)
        self._conversation_back_requests.pop(conversation_id, None)
        self.queues.pop(conversation_id, None)
        self._queue_close_events.pop(conversation_id, None)

    def has_queue(self, conversation_id: str) -> bool:
        return conversation_id in self.queues


webchat_queue_mgr = WebChatQueueMgr()
