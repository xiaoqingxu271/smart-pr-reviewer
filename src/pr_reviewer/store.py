"""任务态与结果缓存的 SQLite 持久化（PRD §6.1 / §6.5）。

为什么不能用进程内存：
- `uvicorn --workers 2` 起多个进程时，提交任务的进程和查询状态的进程不是同一个，
  内存 dict 里的任务对另一个进程不存在 → GET /review/{id} 随机 404；
- 进程重启（部署、崩溃、--reload）丢掉全部任务，用户手里的 review_id 变成孤儿；
- dict 没有上限，结果报告越攒越多，最终吃满内存。

SQLite 是这个规模下的最小足够方案：单文件、零新增服务、自带跨进程写锁。
WAL 让"读状态"不阻塞"写结果"；每次操作用短事务，避免长连接持有写锁。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id         TEXT PRIMARY KEY,
    status     TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    request    TEXT NOT NULL,
    result     TEXT,
    report     TEXT,
    error      TEXT,
    cached     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS result_cache (
    key        TEXT PRIMARY KEY,
    expires_at REAL NOT NULL,
    result     TEXT NOT NULL,
    report     TEXT NOT NULL
);
"""

TERMINAL = ("completed", "failed")


def connect(db_path: Path) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(db_path: Path) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


class TaskStore:
    """任务生命周期：queued → running → completed / failed。"""

    def __init__(self, db_path: Path):
        self.path = Path(db_path)
        init_db(self.path)
        self._conn = connect(self.path)
        self._lock = threading.Lock()      # 同一连接被线程池共享，写操作串行化

    def create(self, task_id: str, request: dict) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO tasks (id, status, created_at, updated_at, request)"
                " VALUES (?, 'queued', ?, ?, ?)",
                (task_id, now, now, json.dumps(request, ensure_ascii=False, sort_keys=True)),
            )
            self._conn.commit()

    def update(self, task_id: str, *, status: str | None = None, result: dict | None = None,
               report: str | None = None, error: str | None = None, cached: bool = False,
               ) -> None:
        sets: list[str] = ["updated_at = ?"]
        values: list[object] = [time.time()]
        for column, value in (("status", status), ("report", report), ("error", error)):
            if value is not None:
                sets.append(f"{column} = ?")
                values.append(value)
        if result is not None:
            sets.append("result = ?")
            values.append(json.dumps(result, ensure_ascii=False))
        if cached:
            sets.append("cached = 1")
        values.append(task_id)
        with self._lock:
            self._conn.execute(
                f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", values)
            self._conn.commit()

    def get(self, task_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT id, status, result, report, error, cached, created_at, updated_at"
            " FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            return None
        return {
            "review_id": row[0], "status": row[1],
            "result": json.loads(row[2]) if row[2] else None,
            "report": row[3], "error": row[4], "cached": bool(row[5]),
            "created_at": row[6], "updated_at": row[7],
        }

    def delete(self, task_id: str) -> None:
        """受理失败时回滚记录，不给调用方留一条永远不动的 queued 任务。"""
        with self._lock:
            self._conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            self._conn.commit()

    def conn_ok(self) -> bool:
        """健康检查用：连接活着且可读（PRD §6.1 /health）。"""
        return self._conn.execute("SELECT 1").fetchone()[0] == 1

    def close(self) -> None:
        self._conn.close()


class ResultCache:
    """结果缓存（PRD §6.5）：key 必须是 **diff 内容身份**，不能是分支名。

    用分支名当 key 的后果：同一分支 force-push 或追加 commit 后 key 不变，
    缓存命中会把**上一版代码**的审查报告当新版返回——审查产品里这属于给出错误结论。
    """

    def __init__(self, db_path: Path, ttl_hours: int = 24):
        self.path = Path(db_path)
        init_db(self.path)
        self.ttl = ttl_hours * 3600
        self._conn = connect(self.path)
        self._lock = threading.Lock()

    @staticmethod
    def make_key(diff_text: str, head_ref: str = "") -> str:
        return hashlib.sha256(f"{head_ref}\n{diff_text}".encode()).hexdigest()

    def get(self, key: str) -> dict | None:
        row = self._conn.execute(
            "SELECT result, report, expires_at FROM result_cache WHERE key = ?",
            (key,)).fetchone()
        if not row:
            return None
        if row[2] < time.time():
            self.evict(key)
            return None
        return {"result": json.loads(row[0]), "report": row[1]}

    def put(self, key: str, result: dict, report: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO result_cache (key, expires_at, result, report)"
                " VALUES (?, ?, ?, ?)",
                (key, time.time() + self.ttl, json.dumps(result, ensure_ascii=False), report),
            )
            # 写入顺带清理过期项，避免缓存无限增长
            self._conn.execute("DELETE FROM result_cache WHERE expires_at < ?", (time.time(),))
            self._conn.commit()

    def evict(self, key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM result_cache WHERE key = ?", (key,))
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()
