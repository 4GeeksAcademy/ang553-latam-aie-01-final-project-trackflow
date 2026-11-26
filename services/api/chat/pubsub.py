"""Single-process fan-out with one bounded queue per WebSocket connection."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from uuid import uuid4

from services.api.chat.contracts import ServerEvent


class SlowSubscriber(RuntimeError):
    pass


_DROPPED = object()


@dataclass(eq=False)
class Subscription:
    session_id: str
    queue: asyncio.Queue = field(repr=False)
    subscription_id: str = field(default_factory=lambda: uuid4().hex)
    closed: bool = False

    async def receive(self) -> ServerEvent:
        item = await self.queue.get()
        if item is _DROPPED:
            self.closed = True
            raise SlowSubscriber("subscriber queue exceeded its bound")
        return item


class SessionPubSub:
    def __init__(self, *, queue_size: int = 64) -> None:
        if queue_size < 1:
            raise ValueError("queue_size must be positive")
        self.queue_size = queue_size
        self._channels: dict[str, set[Subscription]] = {}
        self._lock = asyncio.Lock()

    async def subscribe(self, session_id: str) -> Subscription:
        subscription = Subscription(session_id, asyncio.Queue(self.queue_size))
        async with self._lock:
            self._channels.setdefault(session_id, set()).add(subscription)
        return subscription

    async def unsubscribe(self, subscription: Subscription) -> None:
        async with self._lock:
            subscribers = self._channels.get(subscription.session_id)
            if subscribers is None:
                return
            subscribers.discard(subscription)
            subscription.closed = True
            if not subscribers:
                self._channels.pop(subscription.session_id, None)

    async def publish(self, session_id: str, event: ServerEvent) -> int:
        async with self._lock:
            subscribers = tuple(self._channels.get(session_id, ()))
            delivered = 0
            for subscription in subscribers:
                if subscription.closed:
                    continue
                try:
                    subscription.queue.put_nowait(event)
                    delivered += 1
                except asyncio.QueueFull:
                    # Evict the slow consumer without affecting other subscribers.
                    try:
                        subscription.queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    subscription.queue.put_nowait(_DROPPED)
                    subscription.closed = True
                    self._channels[session_id].discard(subscription)
            if session_id in self._channels and not self._channels[session_id]:
                self._channels.pop(session_id, None)
            return delivered

    async def subscriber_count(self, session_id: str) -> int:
        async with self._lock:
            return len(self._channels.get(session_id, ()))
