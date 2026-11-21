"""In-process API latency / error metrics (no external dependencies).

Per-route latency samples are kept in bounded ring buffers, labelled by the
route *template* (``/inventory/resources``), never the raw URL, so memory and
label cardinality stay bounded. Values are per-process and reset on restart.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable, MutableMapping
from functools import lru_cache
from typing import Any

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

_UNMATCHED = "<unmatched>"


def _percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    k = round(pct / 100 * (len(sorted_values) - 1))
    return sorted_values[max(0, min(len(sorted_values) - 1, k))]


class LatencyRecorder:
    """Thread-safe bounded latency reservoir keyed by (method, route)."""

    def __init__(self, window: int = 1024) -> None:
        self._window = window
        self._lock = threading.Lock()
        self._samples: dict[tuple[str, str], deque[float]] = {}
        self._counts: dict[tuple[str, str], int] = {}
        self._errors: dict[tuple[str, str], int] = {}

    def record(self, method: str, route: str, elapsed_ms: float, status: int) -> None:
        key = (method, route)
        with self._lock:
            bucket = self._samples.get(key)
            if bucket is None:
                bucket = self._samples[key] = deque(maxlen=self._window)
            bucket.append(elapsed_ms)
            self._counts[key] = self._counts.get(key, 0) + 1
            if status >= 500:
                self._errors[key] = self._errors.get(key, 0) + 1

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            items = [
                (key, sorted(samples), self._counts[key], self._errors.get(key, 0))
                for key, samples in self._samples.items()
            ]
        return [
            {
                "method": method,
                "route": route,
                "requests": count,
                "server_errors": errors,
                "window": len(values),
                "p50_ms": round(_percentile(values, 50), 3),
                "p95_ms": round(_percentile(values, 95), 3),
                "max_ms": round(values[-1], 3) if values else 0.0,
            }
            for (method, route), values, count, errors in sorted(items)
        ]

    def reset(self) -> None:
        with self._lock:
            self._samples.clear()
            self._counts.clear()
            self._errors.clear()


@lru_cache
def get_latency_recorder() -> LatencyRecorder:
    return LatencyRecorder()


class LatencyMiddleware:
    """Pure ASGI middleware: times HTTP requests and records them by route."""

    def __init__(self, app: ASGIApp, *, recorder: LatencyRecorder) -> None:
        self.app = app
        self.recorder = recorder

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        status_holder = {"status": 500}

        async def _send(message: Message) -> None:
            if message.get("type") == "http.response.start":
                status_holder["status"] = int(message.get("status", 500))
            await send(message)

        try:
            await self.app(scope, receive, _send)
        finally:
            route = scope.get("route")
            label = getattr(route, "path", None) or _UNMATCHED
            self.recorder.record(
                str(scope.get("method", "")),
                str(label),
                (time.perf_counter() - started) * 1000,
                status_holder["status"],
            )
