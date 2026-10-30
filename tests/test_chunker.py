"""分块测试（PRD §5.2）：AST 边界完整性 + 降级链 + changed_lines 附着。"""
import pytest

from pr_reviewer.chunker import chunk_file, split_oversized_chunk

PY_CODE = '''\
import os

API_KEY = "sk-xxxx"

def small():
    return 1

class Service:
    timeout = 30

    @property
    def name(self):
        return "svc"

    def query(self, uid):
        sql = f"SELECT * FROM users WHERE id={uid}"
        return sql
'''


def test_python_chunks_never_split_functions():
    chunks = chunk_file("app/service.py", PY_CODE, changed_lines=list(range(1, 18)))
    symbols = [c.symbol for c in chunks]
    assert "<module>" in symbols
    assert "Service" in symbols
    assert "Service.query" in symbols
    assert "Service.name" in symbols
    # 每块是完整函数：起点是 def/装饰器行，终点是函数最后一行
    q = next(c for c in chunks if c.symbol == "Service.query")
    assert PY_CODE.splitlines()[q.start_line - 1].strip().startswith("def query")
    assert q.end_line >= q.start_line + 2


def test_decorator_counts_in_start_line():
    chunks = chunk_file("app/service.py", PY_CODE, changed_lines=[14])
    name = next(c for c in chunks if c.symbol == "Service.name")
    assert PY_CODE.splitlines()[name.start_line - 1].strip() == "@property"


def test_changed_lines_attached():
    chunks = chunk_file("app/service.py", PY_CODE, changed_lines=[16])
    q = next(c for c in chunks if c.symbol == "Service.query")
    assert q.changed_lines == [16]
    others = [c for c in chunks if c.symbol != "Service.query"]
    assert all(not c.changed_lines for c in others)


def test_syntax_error_degrades_to_regex():
    broken = "def broken(:\n    pass\n\ndef ok():\n    return 1\n"
    chunks = chunk_file("bad.py", broken, changed_lines=[1])
    assert chunks and all(c.degraded for c in chunks)


def test_plain_text_falls_back_to_line_windows():
    chunks = chunk_file("notes.txt", "line\n" * 300, changed_lines=[120])
    assert chunks and all(c.degraded for c in chunks)


def test_oversized_chunk_splits_with_header_prefix():
    body = "def huge():\n" + "".join(f"    x{i} = {i}\n" for i in range(500))
    from pr_reviewer.models import CodeChunk
    chunk = CodeChunk(file_path="big.py", start_line=1, end_line=501,
                      symbol="huge", language="py", content=body)
    parts = split_oversized_chunk(chunk, token_limit=100)  # ~300 字符/块
    assert len(parts) > 1
    assert all(p.degraded for p in parts)
    assert all(p.content.startswith("def huge():") for p in parts)  # 签名头保留


def test_chunk_id_stable():
    chunks = chunk_file("app/service.py", PY_CODE, changed_lines=[])
    assert len({c.chunk_id for c in chunks}) == len(chunks)
