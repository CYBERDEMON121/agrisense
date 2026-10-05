"""The device-connectivity layer.

``EventBus`` is a small, dependency-free MQTT-style broker.  Every simulated device
publishes to hierarchical topics using the same naming convention a real fleet
would use, so swapping the simulated transport for paho-mqtt only requires
implementing :class:`BusTransport`.

Topic grammar::

    agri/<farm_id>/<group>/<device_id>/<channel>     e.g. agri/green-valley-01/soil/SM-A-03/telemetry
    farm/<farm_id>/<area>/<kind>                     e.g. farm/green-valley-01/North Field/plot

Subscriptions support ``+`` (single level) and ``#`` (multi level) wildcards, the
same matching rules brokers use, so downstream consumers are portable.
"""

from __future__ import annotations

import fnmatch
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

Handler = Callable[[str, dict[str, Any], "Message"], None]

TOPIC_SEP = "/"


def topic_matches(filter_: str, topic: str) -> bool:
    """MQTT-style topic filter matching (``+`` and ``#``)."""
    if "#" not in filter_ and "+" not in filter_:
        return filter_ == topic
    f = filter_.split(TOPIC_SEP)
    t = topic.split(TOPIC_SEP)
    i = 0
    for i, part in enumerate(f):
        if part == "#":
            return i <= len(t)
        if i >= len(t):
            return False
        if part != "+" and part != t[i]:
            return False
    return len(f) == len(t)


def parse_topic(topic: str) -> dict[str, str]:
    """Split a topic into its named parts for convenient consumption."""
    parts = topic.split(TOPIC_SEP)
    out: dict[str, str] = {"topic": topic}
    if len(parts) >= 6 and parts[0] == "agri":
        out.update(farm=parts[1], group=parts[2], device=parts[3], channel=TOPIC_SEP.join(parts[4:]))
    return out


@dataclass(slots=True)
class Message:
    topic: str
    payload: dict[str, Any]
    ts: float
    qos: int = 0
    retain: bool = False
    meta: dict[str, Any] = field(default_factory=dict)


class EventBus:
    """Thread-safe publish/subscribe hub with retain + replay support."""

    def __init__(self, farm_id: str, replay_size: int = 512) -> None:
        self.farm_id = farm_id
        self._subs: dict[str, list[Handler]] = {}
        self._retained: dict[str, Message] = {}
        self._history: deque[Message] = deque(maxlen=replay_size)
        self._lock = threading.RLock()
        self.published = 0
        self.delivered = 0
        self.errors = 0

    # ---- producer side ------------------------------------------------
    def topic(self, group: str, device: str, channel: str) -> str:
        return TOPIC_SEP.join(["agri", self.farm_id, group, device, channel])

    def publish(self, topic: str, payload: dict[str, Any], *, qos: int = 0,
                retain: bool = False, ts: float | None = None) -> Message:
        msg = Message(topic=topic, payload=payload, ts=ts if ts is not None else time.time(),
                      qos=qos, retain=retain)
        with self._lock:
            self.published += 1
            self._history.append(msg)
            if retain:
                self._retained[topic] = msg
            targets = self._match(topic)
        for handler in targets:
            try:
                handler(topic, payload, msg)
                self.delivered += 1
            except Exception:  # a bad subscriber must never kill the sim tick
                self.errors += 1
        return msg

    # ---- consumer side ------------------------------------------------
    def subscribe(self, topic_filter: str, handler: Handler) -> Callable[[], None]:
        with self._lock:
            self._subs.setdefault(topic_filter, []).append(handler)

        def unsubscribe() -> None:
            with self._lock:
                handlers = self._subs.get(topic_filter)
                if handlers and handler in handlers:
                    handlers.remove(handler)

        return unsubscribe

    def _match(self, topic: str) -> list[Handler]:
        out: list[Handler] = []
        for filt, handlers in self._subs.items():
            if topic_matches(filt, topic):
                out.extend(handlers)
        return out

    # ---- retained / replay -------------------------------------------
    def retained(self, prefix: str = "") -> list[Message]:
        with self._lock:
            return [m for t, m in self._retained.items() if t.startswith(prefix)]

    def history(self, topic_prefix: str | None = None, limit: int = 100) -> list[Message]:
        with self._lock:
            items = list(self._history)
        if topic_prefix:
            items = [m for m in items if m.topic.startswith(topic_prefix)]
        return items[-limit:]

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "transport": "simulated",
                "published": self.published,
                "delivered": self.delivered,
                "errors": self.errors,
                "subscriptions": len(self._subs),
                "retained": len(self._retained),
                "ring_buffer": len(self._history),
            }


class MqttTransport:
    """Adapter seam: use a real broker when available, else stay in-process.

    With ``paho-mqtt`` installed and ``settings.mqtt_broker`` set, hardware and
    simulated devices can share one broker.  Without it the bus stays fully
    simulated, which is why every consumer only depends on :class:`EventBus`.
    """

    def __init__(self, bus: EventBus, broker: str | None, port: int = 1883) -> None:
        self.bus = bus
        self.broker = broker
        self.port = port
        self.mode = "simulated"
        self.client = None
        if broker:
            try:  # pragma: no cover - depends on optional dependency
                import paho.mqtt.client as mqtt  # type: ignore

                self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
                self.client.connect(broker, port, keepalive=60)
                self.client.loop_start()
                self.mode = "mqtt"
            except Exception:
                self.client = None
                self.mode = "simulated"

    def forward(self, message: Message) -> None:  # pragma: no cover - optional path
        if self.client is not None:
            import json

            self.client.publish(message.topic, json.dumps(message.payload), qos=message.qos)

    def info(self) -> dict[str, Any]:
        return {"mode": self.mode, "broker": self.broker, "port": self.port}


def topic_wildcard(group: str = "+", device: str = "+", channel: str = "#") -> str:
    return TOPIC_SEP.join(["agri", "+", group, device, channel])


def glob_match(pattern: str, value: str) -> bool:
    """Convenience wrapper used by the device registry lookups."""
    return fnmatch.fnmatch(value, pattern)


def iter_devices(messages: Iterable[Message]) -> set[str]:
    return {parse_topic(m.topic).get("device", "") for m in messages}