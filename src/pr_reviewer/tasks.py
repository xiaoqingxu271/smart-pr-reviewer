"""有界任务执行器（PRD §6.1）：替换不设上限的 FastAPI BackgroundTasks。

BackgroundTasks 把每个请求直接丢进 anyio 的默认线程池（约 40 个线程）且不设排队
上限：一个几百文件的 PR 会长时间占住一个线程，突发提交能把线程池占满，后果是所有
请求一起变慢直到超时。有界池 + 待处理上限的意义不是"跑得更快"，而是过载时**拒绝得
干脆**（可重试的 429），而不是让整个服务一起淹死。
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor


class QueueFull(RuntimeError):
    """待处理任务已满：调用方应当稍后重试，而不是继续压进来。"""


class TaskRunner:
    def __init__(self, max_workers: int = 2, max_pending: int = 20):
        if max_workers < 1 or max_pending < 1:
            raise ValueError("max_workers / max_pending 必须 >= 1")
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="review")
        self._workers = max_workers
        self._max_pending = max_pending
        self._lock = threading.Lock()
        self._pending = 0        # 已受理但未完成（排队中 + 执行中）
        self._rejected = 0

    @property
    def pending(self) -> int:
        with self._lock:
            return self._pending

    @property
    def stats(self) -> dict:
        with self._lock:
            return {"pending": self._pending, "max_pending": self._max_pending,
                    "workers": self._workers, "rejected_total": self._rejected}

    def submit(self, fn, *args) -> None:
        """受理一个任务；已满则抛 QueueFull，且不留下任何状态。"""
        with self._lock:
            if self._pending >= self._max_pending:
                self._rejected += 1
                raise QueueFull(
                    f"待处理审查任务已达上限（{self._max_pending}），请稍后重试")
            self._pending += 1

        def _runner() -> None:
            try:
                fn(*args)
            finally:
                with self._lock:
                    self._pending -= 1

        try:
            self._pool.submit(_runner)
        except Exception:                       # 池已关闭等：不留下占位计数
            with self._lock:
                self._pending -= 1
            raise

    def shutdown(self, wait: bool = False) -> None:
        self._pool.shutdown(wait=wait)
