"""Live event bus over Redis pub/sub (E2.4).

The copilot has two processes that both need to push updates to a connected client:

* the **API** (FastAPI) runs the chat loop -> ``message.delta`` / ``tool.started`` /
  ``tool.finished`` / ``refusal`` / ``workspace.updated``;
* the **RQ worker** runs the (async) design loop -> ``run.status`` transitions
  (``queued`` -> ``running`` -> ``completed`` / ``failed``) + ``workspace.updated``.

Both publish to a per-job Redis channel; the SSE endpoint (``GET /jobs/{id}/events``)
subscribes to that channel and fans every event out to the browser. Because the fan-out
goes through Redis, it does not matter which process produced the event — any connected
client streaming that job sees it.

Design notes / invariants:
- Publishing is **best-effort**: a broker hiccup must never break a design run or a
  chat turn, so :meth:`EventBus.publish` swallows every error and returns ``None``.
- Events carry only JSON-native data (coerced via :func:`saas.serialization.to_jsonable`
  so numpy / NaN in a workspace snapshot round-trips). The event payload never contains
  a number the deterministic tools did not compute — the workspace is reflect-only.
- Off by default (``COPILOT_EVENTS``): host tools / the OpenAI-free test-suite never open
  a Redis connection. Tests inject an :class:`EventBus` built over ``fakeredis`` directly.
- The LLM never authors events: event *types* are a fixed enum defined here, and every
  payload is assembled by backend code.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any, Iterator

from .config import get_settings
from .serialization import to_jsonable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from redis import Redis
    from redis.client import PubSub

# --------------------------------------------------------------------------- #
# Event types (fixed enum — the frontend switches on these; LLM never sets them)
# --------------------------------------------------------------------------- #
EVENT_MESSAGE_DELTA = "message.delta"
EVENT_TOOL_STARTED = "tool.started"
EVENT_TOOL_FINISHED = "tool.finished"
EVENT_WORKSPACE_UPDATED = "workspace.updated"
EVENT_RUN_STATUS = "run.status"
EVENT_REFUSAL = "refusal"
EVENT_ERROR = "error"

EVENT_TYPES: tuple[str, ...] = (
    EVENT_MESSAGE_DELTA,
    EVENT_TOOL_STARTED,
    EVENT_TOOL_FINISHED,
    EVENT_WORKSPACE_UPDATED,
    EVENT_RUN_STATUS,
    EVENT_REFUSAL,
    EVENT_ERROR,
)

_CHANNEL_PREFIX = "copilot:events"


class EventBus:
    """Publish/subscribe helper for per-job live events over Redis pub/sub.

    A single connection is reused for all publishes. Subscribers get their own
    :class:`~redis.client.PubSub` (Redis requires this). Both real ``redis`` and
    ``fakeredis`` connections work — the test-suite injects the latter.
    """

    def __init__(
        self,
        connection: "Redis | None" = None,
        *,
        channel_prefix: str = _CHANNEL_PREFIX,
    ) -> None:
        self._conn = connection
        self._prefix = channel_prefix

    # ------------------------------------------------------------------ #
    @property
    def connection(self) -> "Redis":
        if self._conn is None:
            # Reuse the queue's process-wide connection so the worker + API share one.
            from .queue import get_redis_connection

            self._conn = get_redis_connection()
        return self._conn

    def channel(self, job_id: str) -> str:
        return f"{self._prefix}:{job_id}"

    # ------------------------------------------------------------------ #
    # Publish (called from the chat loop / service / worker)
    # ------------------------------------------------------------------ #
    def build_event(
        self, job_id: str, event_type: str, data: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Assemble the canonical event envelope (also used when yielding locally)."""
        return {
            "type": event_type,
            "job_id": job_id,
            "ts": time.time(),
            "data": to_jsonable(data or {}),
        }

    def publish(
        self, job_id: str, event_type: str, data: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        """Publish one event to the job's channel. Best-effort: never raises.

        Returns the event dict on success, or ``None`` if publishing failed (e.g. the
        broker is unreachable) — the caller's main flow must not depend on it.
        """
        event = self.build_event(job_id, event_type, data)
        try:
            payload = json.dumps(event, default=str)
            self.connection.publish(self.channel(job_id), payload)
        except Exception:  # noqa: BLE001 - publishing is best-effort by contract
            return None
        return event

    # ------------------------------------------------------------------ #
    # Subscribe (called from the SSE endpoint / tests)
    # ------------------------------------------------------------------ #
    def subscribe(self, job_id: str) -> "PubSub":
        """Return a PubSub already subscribed to the job channel.

        The caller owns it and must ``close()`` it (the SSE endpoint / test does so).
        ``ignore_subscribe_messages=True`` means :meth:`get_message` only yields real
        published messages.
        """
        pubsub = self.connection.pubsub(ignore_subscribe_messages=True)
        pubsub.subscribe(self.channel(job_id))
        return pubsub

    def listen(
        self,
        job_id: str,
        *,
        poll_timeout: float = 1.0,
        max_events: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield decoded event dicts for a job as they arrive (blocking generator).

        Used by tests and (via a threadpool) by the SSE endpoint. ``max_events`` bounds
        the generator for tests; ``None`` streams until the consumer stops iterating.
        """
        pubsub = self.subscribe(job_id)
        seen = 0
        try:
            while max_events is None or seen < max_events:
                message = pubsub.get_message(timeout=poll_timeout)
                event = _decode_message(message)
                if event is None:
                    continue
                yield event
                seen += 1
        finally:
            try:
                pubsub.close()
            except Exception:  # noqa: BLE001
                pass


def _decode_message(message: dict[str, Any] | None) -> dict[str, Any] | None:
    """Decode a raw redis pub/sub message into an event dict, or ``None`` to skip."""
    if not message or message.get("type") != "message":
        return None
    raw = message.get("data")
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    if not isinstance(raw, str):
        return None
    try:
        decoded = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return decoded if isinstance(decoded, dict) else None


# --------------------------------------------------------------------------- #
# Active bus (None when events are disabled -> publishing is a no-op)
# --------------------------------------------------------------------------- #
def get_event_bus() -> EventBus | None:
    """Return the process event bus, or ``None`` when events are disabled.

    Disabled by default so host tools / tests never touch Redis. Callers treat ``None``
    as "no-op" (see :func:`publish_event`).
    """
    if not get_settings().events_enabled:
        return None
    return EventBus()


def publish_event(
    job_id: str, event_type: str, data: dict[str, Any] | None = None
) -> None:
    """Publish via the active bus if events are enabled; otherwise do nothing."""
    bus = get_event_bus()
    if bus is not None:
        bus.publish(job_id, event_type, data)


__all__ = [
    "EVENT_ERROR",
    "EVENT_MESSAGE_DELTA",
    "EVENT_REFUSAL",
    "EVENT_RUN_STATUS",
    "EVENT_TOOL_FINISHED",
    "EVENT_TOOL_STARTED",
    "EVENT_TYPES",
    "EVENT_WORKSPACE_UPDATED",
    "EventBus",
    "get_event_bus",
    "publish_event",
]
