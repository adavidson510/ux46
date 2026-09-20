"""Bounded invalidation events, shared by the native adapters.

Events are hints to read authoritative state, not a durable transcript. A cursor
only acknowledges the returned page; a lost interval explicitly asks readers
to reconcile snapshots. Epochs distinguish two processes with equal cursors.
"""
import secrets
import threading
import time


class EventLog:
    def __init__(self, limit: int = 500):
        self.limit = max(1, limit)
        self.epoch = secrets.token_hex(16)
        self._events: list[dict] = []
        self._seq = 0
        self._cond = threading.Condition()

    @property
    def seq(self) -> int:
        with self._cond:
            return self._seq

    def publish(self, event: dict) -> int:
        with self._cond:
            self._seq += 1
            self._events.append(dict(event, seq=self._seq, at=time.time()))
            del self._events[:max(0, len(self._events) - self.limit)]
            self._cond.notify_all()
            return self._seq

    def since(self, after: int, timeout: float = 25.0, room: str = "",
              epoch: str = "") -> dict:
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                oldest = self._events[0]["seq"] if self._events else self._seq + 1
                reason = ("epoch_changed" if epoch and epoch != self.epoch else
                          "cursor_ahead" if after > self._seq else
                          "events_expired" if after < oldest - 1 else "")
                start = oldest - 1 if reason else after
                events = [e for e in self._events if e["seq"] > start]
                if room:
                    events = [e for e in events if e.get("room") in (room, None, "")
                              or room in (e.get("rooms") or []) or e.get("global")]
                if events or reason or time.monotonic() >= deadline:
                    page = events[:100]
                    more = len(events) > len(page)
                    return {"seq": page[-1]["seq"] if more else self._seq,
                            "events": page, "epoch": self.epoch, "more": more,
                            "gap": bool(reason), "reason": reason,
                            "latest_seq": self._seq}
                self._cond.wait(timeout=max(0, deadline - time.monotonic()))
