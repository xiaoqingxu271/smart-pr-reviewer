"""审查结果缓存（PRD §6.5）：内存 TTL 缓存，key = 请求的规范 JSON hash。"""
from __future__ import annotations

import hashlib
import json
import time


class TTLCache:
    def __init__(self, ttl_hours: int = 24):
        self.ttl = ttl_hours * 3600
        self._store: dict[str, tuple[float, object]] = {}

    @staticmethod
    def make_key(payload: dict) -> str:
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()

    def get(self, key: str):
        item = self._store.get(key)
        if not item:
            return None
        at, value = item
        if time.time() - at > self.ttl:
            self._store.pop(key, None)
            return None
        return value

    def put(self, key: str, value: object) -> None:
        self._store[key] = (time.time(), value)
