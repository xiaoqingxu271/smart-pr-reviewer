"""任务态存储（PRD §6.1/§6.5）：schema 升级与写回结果落库。

单列出来测是因为这两件事在 API 测试里看不见：
- 老库（没有 publish 列）必须能原地升级——部署时 `.data/reviews.sqlite3` 是留在磁盘上的，
  CREATE TABLE IF NOT EXISTS 不会补列，直接起服务会在第一次 INSERT 就炸；
- publish 字段是"评论发没发"的唯一对外凭据，丢了就等于状态说谎。
"""
import sqlite3

from pr_reviewer.store import TaskStore

LEGACY_SCHEMA = """
CREATE TABLE tasks (
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
"""


def make_legacy_db(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(LEGACY_SCHEMA)
    conn.execute("INSERT INTO tasks (id, status, created_at, updated_at, request)"
                 " VALUES ('old', 'completed', 1, 1, '{}')")
    conn.commit()
    conn.close()


def test_legacy_db_gains_publish_column_and_keeps_rows(tmp_path):
    db = tmp_path / ".data" / "reviews.sqlite3"
    make_legacy_db(db)

    store = TaskStore(db)
    assert store.get("old")["publish"] is None          # 旧行读得到，列值为 NULL
    store.update("old", publish={"status": "posted", "inline": 2})
    assert store.get("old")["publish"] == {"status": "posted", "inline": 2}
    store.close()

    # 重开一次：列已在，init_db 不该重复 ALTER（重复 ADD COLUMN 会报错）
    again = TaskStore(db)
    assert again.get("old")["publish"]["inline"] == 2
    again.close()
