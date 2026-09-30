"""只追加事件日志。

事件一经写入不可改写：每条事件携带前一条事件的哈希，形成哈希链。
任何对历史事件的改动都会在 verify_chain 中被发现（TamperError）。
持久化采用单行 JSON 的 JSONL，崩溃时至少保留已落盘的完整行。
"""
from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any, Iterable

from .clock import Clock, SystemClock, to_iso
from .errors import ConflictError, TamperError
from .models import Event

GENESIS = "0" * 64


def canonical_hash(event: Event) -> str:
    body = {
        "seq": event.seq,
        "at": event.at,
        "kind": event.kind,
        "mentor_id": event.mentor_id,
        "payload": event.payload,
        "prev_hash": event.prev_hash,
        "actor": event.actor,
        "command_id": event.command_id,
    }
    raw = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class EventLog:
    """内存 + 可选 JSONL 文件的只追加日志。"""

    def __init__(self, path: str | Path | None = None, clock: Clock | None = None):
        self._path = Path(path) if path else None
        self._clock = clock or SystemClock()
        self._events: list[Event] = []
        self._command_ids: set[str] = set()
        self._lock = threading.RLock()
        if self._path and self._path.exists():
            self._load()

    # -- 读取 ---------------------------------------------------------------

    @property
    def events(self) -> tuple[Event, ...]:
        with self._lock:
            return tuple(self._events)

    def events_for(self, mentor_id: str) -> tuple[Event, ...]:
        with self._lock:
            return tuple(e for e in self._events if e.mentor_id == mentor_id)

    def head_seq(self) -> int:
        with self._lock:
            return len(self._events)

    # -- 写入 ---------------------------------------------------------------

    def append(
        self,
        kind: str,
        mentor_id: str,
        payload: dict[str, Any],
        actor: str = "",
        command_id: str = "",
    ) -> Event:
        with self._lock:
            if command_id:
                if command_id in self._command_ids:
                    raise ConflictError(f"命令已处理：{command_id}")
            seq = len(self._events) + 1
            prev_hash = self._events[-1].event_hash if self._events else GENESIS
            event = Event(
                seq=seq,
                at=to_iso(self._clock.now()),
                kind=kind,
                mentor_id=mentor_id,
                payload=payload,
                prev_hash=prev_hash,
                actor=actor,
                command_id=command_id,
            )
            object.__setattr__(event, "event_hash", canonical_hash(event))
            self._persist(event)
            self._events.append(event)
            if command_id:
                self._command_ids.add(command_id)
            return event

    # -- 完整性 -------------------------------------------------------------

    def verify_chain(self) -> None:
        """重算整条哈希链，任何历史改写都抛出 TamperError。"""
        with self._lock:
            prev = GENESIS
            for event in self._events:
                if event.prev_hash != prev:
                    raise TamperError(f"事件 {event.seq} 前向哈希不连续")
                if canonical_hash(event) != event.event_hash:
                    raise TamperError(f"事件 {event.seq} 内容哈希不匹配")
                prev = event.event_hash

    def _persist(self, event: Event) -> None:
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()

    def _load(self) -> None:
        for lineno, line in enumerate(self._path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                event = Event.from_dict(json.loads(line))
            except (json.JSONDecodeError, KeyError) as exc:
                raise TamperError(f"日志第 {lineno} 行无法解析") from exc
            self._events.append(event)
            if event.command_id:
                self._command_ids.add(event.command_id)
        self.verify_chain()
