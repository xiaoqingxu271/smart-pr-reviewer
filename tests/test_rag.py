"""RAG 测试（PRD §5.3）：条款切块、索引指纹、检索判据、空结果、has_rule。

全部离线运行：InMemoryStore + HashEmbedder / 可控向量的 TableEmbedder。
hash 词袋分数天然偏低（top1 均分 0.137），这里的 threshold/margin 是 hash 口径，
不代表生产标定值（生产用 local bge，见 experiments/rag-embedding-bench.md）。
"""
import json
import math
from pathlib import Path

import pytest

from pr_reviewer.config import Settings
from pr_reviewer.rag import (
    INDEX_META_FILE,
    HashEmbedder,
    InMemoryStore,
    StandardsRetriever,
    _with_prefix,
    parse_standards_md,
)

SAMPLE_MD = """\
# 研发代码安全规范（样例）

> 说明文字，不入库

## SEC-001 禁止硬编码密钥与密码

任何密码、API Key、Token 不得硬编码在源码中，必须通过环境变量获取。

## SEC-002 SQL 查询禁止字符串拼接

禁止使用 f-string 拼接 SQL 语句，必须使用参数化查询。

## SEC-004 子进程调用必须限制 shell

subprocess 禁止 shell=True 且命令参数来自用户输入。

### 附注小节

子进程白名单校验示例。

## SEC-006 敏感信息输出脱敏

日志中不得输出明文密码与完整 token。
"""


# hash 词袋的分数口径：top1 均分仅 0.137，门限必须比真实模型低一个档
HASH_THRESHOLD = 0.30
HASH_MARGIN = 0.20      # 放宽到不裁尾，保持原有断言语义


def make_settings(tmp_path: Path, **kw) -> Settings:
    base = {"_env_file": None, "chroma_dir": tmp_path / ".chroma",
            "embedding_backend": "hash",
            "retrieval_score_threshold": HASH_THRESHOLD,
            "retrieval_margin": HASH_MARGIN}
    base.update(kw)
    return Settings(**base)


def make_retriever(tmp_path: Path) -> StandardsRetriever:
    return StandardsRetriever(make_settings(tmp_path),
                              store=InMemoryStore(tmp_path / "store.json"),
                              embedder=HashEmbedder())


def write_md(tmp_path: Path, text: str = SAMPLE_MD) -> Path:
    p = tmp_path / "standards.md"
    p.write_text(text, encoding="utf-8")
    return p


def test_parse_rules_by_heading():
    rules = parse_standards_md(SAMPLE_MD)
    ids = [r["rule_id"] for r in rules]
    assert ids == ["SEC-001", "SEC-002", "SEC-004", "SEC-006"]
    assert "环境变量" in rules[0]["text"]
    # "### 附注小节" 是更深层级标题：作为 SEC-004 的边界，附注内容不入库
    assert "白名单校验" not in rules[2]["text"]


FENCED_MD = """\
# 规范（样例）

## SEC-002 SQL 查询禁止字符串拼接

禁止拼接。

```python
# 错误写法
cursor.execute("SELECT * FROM t WHERE name='" + name + "'")
```

必须使用参数化查询。

## SEC-003 禁止不安全的反序列化

正文内容。
"""


def test_parse_keeps_code_fence_content():
    """条款带示例代码后，围栏内的 `# 注释` 不得被当成块边界（否则正文静默截断）。"""
    rules = parse_standards_md(FENCED_MD)
    assert [r["rule_id"] for r in rules] == ["SEC-002", "SEC-003"]
    text = rules[0]["text"]
    assert "# 错误写法" in text
    assert "必须使用参数化查询" in text, "条款正文不得被示例代码截断"
    assert "```" in text


def test_build_and_retrieve_relevance(tmp_path):
    retriever = make_retriever(tmp_path)
    md = write_md(tmp_path)
    count = retriever.build_index(md)
    assert count == 4

    hits = retriever.retrieve("cursor.execute(sql) 用户输入 SQL 注入 拼接 f-string")
    assert hits, "相关查询应有命中"
    assert hits[0].rule_id == "SEC-002"
    assert all(h.score >= 0.35 for h in hits)
    assert len(hits) <= 3  # top_k=3

    hits2 = retriever.retrieve("API_KEY = 'sk-123' 硬编码 password")
    assert hits2 and hits2[0].rule_id == "SEC-001"


def test_empty_result_below_threshold(tmp_path):
    retriever = make_retriever(tmp_path)
    retriever.build_index(write_md(tmp_path))
    assert retriever.retrieve("完全无关的早餐菜谱 粥 油条 豆浆") == []


def test_hash_unchanged_rebuild_skips(tmp_path):
    retriever = make_retriever(tmp_path)
    md = write_md(tmp_path)
    assert retriever.build_index(md) == 4
    store_path = tmp_path / "store.json"
    first_mtime = store_path.stat().st_mtime_ns
    # 未变化的重建：返回条款数且不改写存储
    assert retriever.build_index(md) == 4
    assert store_path.stat().st_mtime_ns == first_mtime


def test_has_rule_and_rule_text(tmp_path):
    retriever = make_retriever(tmp_path)
    retriever.build_index(write_md(tmp_path))
    assert retriever.has_rule("SEC-002")
    assert not retriever.has_rule("SEC-999")   # 幻觉条款号
    assert "参数化查询" in retriever.rule_text("SEC-002")
    assert retriever.rule_text("SEC-999") is None


def test_index_update_on_change(tmp_path):
    retriever = make_retriever(tmp_path)
    md = write_md(tmp_path)
    retriever.build_index(md)
    updated = SAMPLE_MD.replace(
        "## SEC-006 敏感信息输出脱敏",
        "## SEC-006 敏感信息输出脱敏\n\n补充：报告中展示密钥必须脱敏。\n\n## SEC-007 弱哈希禁用\n\n密码存储禁用 md5。",
    )
    md.write_text(updated, encoding="utf-8")
    assert retriever.build_index(md) == 5       # hash 变化 → 重建
    assert retriever.has_rule("SEC-007")


# ---------------------------------------------------------------- 检索过滤判据

class TableEmbedder:
    """可控向量：cos(query_vec, doc_vec) 由测试直接给定，用来单独验证判据逻辑。"""

    def __init__(self, query_vec: list[float], name: str = "table",
                 docs: dict[str, list[float]] | None = None, query_prefix: str = ""):
        self.query_vec, self.name = query_vec, name
        self.docs = docs or {}
        self.query_prefix = query_prefix
        self.doc_inputs: list[list[str]] = []
        self.query_inputs: list[str] = []

    def embed(self, texts):
        self.doc_inputs.append(list(texts))
        return [self.docs.get(t, self.query_vec) for t in texts]

    def embed_query(self, text):
        self.query_inputs.append(text)
        return self.query_vec


def seeded(tmp_path: Path, scores: dict[str, float], store_name: str = "seed.json",
           **settings_kw) -> StandardsRetriever:
    """按目标相似度灌库：query=[1,0]，doc=[s,√(1-s²)] ⇒ cosine 恰为 s。"""
    store = InMemoryStore(tmp_path / store_name)
    retriever = StandardsRetriever(make_settings(tmp_path, **settings_kw),
                                   store=store, embedder=TableEmbedder([1.0, 0.0]))
    ids = list(scores)
    store.add(ids=ids,
              embeddings=[[s, math.sqrt(max(0.0, 1 - s * s))] for s in scores.values()],
              documents=[f"doc-{r}" for r in ids],
              metadatas=[{"rule_id": r, "title": r} for r in ids])
    return retriever


def test_gate_rejects_when_top1_below_threshold(tmp_path):
    """门限只判 top1：冠军不够相关就整体返回 []（PRD §5.3 空结果行为）。"""
    retriever = seeded(tmp_path, {"SEC-001": 0.40, "SEC-002": 0.30},
                       retrieval_score_threshold=0.45)
    assert retriever.retrieve("subprocess.run(cmd, shell=True)") == []


def test_margin_trims_tail_not_gate(tmp_path):
    """margin 决定"除冠军外还附带几条"，不改变 top1 是否入选。"""
    scores = {"SEC-001": 0.60, "SEC-002": 0.56, "SEC-003": 0.50}
    tight = seeded(tmp_path, scores, store_name="tight.json",
                   retrieval_score_threshold=0.40, retrieval_margin=0.02)
    assert [h.rule_id for h in tight.retrieve("q")] == ["SEC-001"]
    loose = seeded(tmp_path, scores, store_name="loose.json",
                   retrieval_score_threshold=0.40, retrieval_margin=0.10)
    assert [h.rule_id for h in loose.retrieve("q")] == ["SEC-001", "SEC-002", "SEC-003"]


def test_absolute_threshold_alone_cannot_separate(tmp_path):
    """绝对阈值的结构性缺陷（改用 top1 门限 + margin 的原因）：
    把门限抬到能挡住干净块，就会同时丢掉分数落在该带内的正确条款。
    """
    correct = 0.388     # 实测 pr-01 硬编码密钥的 top1 分数
    clean_band = 0.412  # 实测干净代码块的 top1 中位数
    keep = seeded(tmp_path, {"SEC-001": correct}, store_name="keep.json",
                  retrieval_score_threshold=correct)
    assert [h.rule_id for h in keep.retrieve("q")] == ["SEC-001"]
    leak = seeded(tmp_path, {"SEC-001": clean_band}, store_name="leak.json",
                  retrieval_score_threshold=correct)
    assert leak.retrieve("干净代码"), "同一门限挡不住分数带内的无关块"


def test_query_prefix_applies_to_query_side_only():
    assert _with_prefix("q", "PRE：") == "PRE：q"
    assert _with_prefix("q", "") == "q"
    emb = HashEmbedder(dim=64)
    emb.query_prefix = "为这个句子生成表示以用于检索相关文章："
    raw = HashEmbedder(dim=64).embed(["sql 注入"])[0]
    assert emb.embed(["sql 注入"])[0] == raw, "doc 侧不应带指令"
    assert emb.embed_query("sql 注入") != raw, "query 侧应带指令"


# ---------------------------------------------------------------- 索引指纹

def test_index_meta_records_embedder_and_dim(tmp_path):
    md = write_md(tmp_path)
    (tmp_path / ".chroma").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".chroma" / ".standards.sha256").write_text("旧版侧文件", encoding="utf-8")
    retriever = make_retriever(tmp_path)
    retriever.build_index(md)

    meta = json.loads((tmp_path / ".chroma" / INDEX_META_FILE).read_text(encoding="utf-8"))
    assert meta["embedder"] == "hash-bow-256d"
    assert meta["dim"] == 256
    assert meta["rule_count"] == 4
    assert not (tmp_path / ".chroma" / ".standards.sha256").exists(), "旧版侧文件应被清理"


def test_index_rebuilt_when_embedder_changes(tmp_path):
    """换 embedding 模型 = 换向量空间，指纹不符必须重建，不能沿用旧向量。"""
    md = write_md(tmp_path)
    store_path = tmp_path / "store.json"
    StandardsRetriever(make_settings(tmp_path), store=InMemoryStore(store_path),
                       embedder=HashEmbedder()).build_index(md)
    assert len(json.loads(store_path.read_text(encoding="utf-8"))[0]["embedding"]) == 256

    emb2 = TableEmbedder([1.0, 0.0], name="st:bge-base-zh-v1.5")
    StandardsRetriever(make_settings(tmp_path), store=InMemoryStore(store_path),
                       embedder=emb2).build_index(md)
    assert emb2.doc_inputs, "换模型后应重新编码条款"
    items = json.loads(store_path.read_text(encoding="utf-8"))
    assert len(items[0]["embedding"]) == 2, "旧维度向量不得残留"

    emb3 = TableEmbedder([1.0, 0.0], name="st:bge-base-zh-v1.5")
    StandardsRetriever(make_settings(tmp_path), store=InMemoryStore(store_path),
                       embedder=emb3).build_index(md)
    assert emb3.doc_inputs == [], "同模型同文档应跳过编码"


def test_retrieve_heals_index_built_by_another_embedder(tmp_path):
    """换了 embedding 后端启动时，检索要自愈重建，而不是把整份审查判失败。

    真实事故：磁盘上是 bge 的 768 维索引，服务因缺 sentence-transformers 降级成
    256 维 hash 词袋——查询向量与旧向量维度不同，_cosine 的 strict zip 抛 ValueError，
    webhook 触发的第一次真实审查就挂在这里。以前只有 build_index 比指纹，
    而服务只调 retrieve，等于把"防混库"设计留在了没人走的分支上。
    """
    md = write_md(tmp_path)
    store_path = tmp_path / "heal.json"
    big = StandardsRetriever(make_settings(tmp_path), store=InMemoryStore(store_path),
                             embedder=TableEmbedder([1.0] * 8, name="st:bge-fake"))
    assert big.build_index(md) == 4
    assert big.stale is False

    small = StandardsRetriever(make_settings(tmp_path), store=InMemoryStore(store_path),
                               embedder=TableEmbedder([1.0, 0.0], name="hash-bow-2d"))
    assert small.stale is True                       # /health 要能看见，运维不用等审查失败
    hits = small.retrieve("subprocess.run(cmd, shell=True)")   # 不该抛 ValueError
    assert isinstance(hits, list)
    assert small.stale is False                      # 已按当前后端重建
    assert len(small._load()[0]["embedding"]) == 2   # 库里的向量换成了 2 维


def test_manual_store_without_fingerprint_is_left_alone(tmp_path):
    """没有指纹文件时只认"维度不同"这条硬证据；维度相同就不覆盖别人的数据。

    测试与脚本会手工往 store 里灌向量。这种情况下猜"是不是同一个模型"没有依据，
    贸然重建会把人家灌进去的数据清掉——所以只在维度对不上时才动手。
    """
    store = InMemoryStore(tmp_path / "manual.json")
    retriever = StandardsRetriever(make_settings(tmp_path), store=store,
                                   embedder=TableEmbedder([1.0, 0.0]))
    store.add(ids=["SEC-001"], embeddings=[[0.0, 1.0]], documents=["手工数据"],
              metadatas=[{"rule_id": "SEC-001", "title": "T"}])
    assert retriever.stale is False                  # 维度一致 → 不动它
    assert retriever._load()[0]["document"] == "手工数据"


def test_parse_accepts_cwe_style_rule_ids(tmp_path):
    """条款号前缀不能写死 SEC-：换成 CWE 官方目录时会被静默解析成 0 条。

    0 条不报错，只是检索永远空转——比直接抛异常危险得多。
    """
    md = tmp_path / "cwe.md"
    md.write_text(
        "# CWE 节选\n\n"
        "## CWE-0089 SQL 命令拼接\n\n程序使用动态构造的 SQL 语句执行查询。\n\n"
        "## CWE-89 变体标题（编号位数不足）\n\n不该被解析。\n\n",
        encoding="utf-8")
    rules = parse_standards_md(md.read_text(encoding="utf-8"))
    # 位数不锁死：CWE-0089 与 CWE-89 都算条款号（语料由脚本统一补零，见 build_cwe_corpus）
    assert [r["rule_id"] for r in rules] == ["CWE-0089", "CWE-89"]


def test_duplicate_rule_ids_are_rejected(tmp_path):
    """同一编号两条正文 → 检索返回哪条由顺序决定，报告里的依据就不可复现。"""
    md = tmp_path / "dup.md"
    md.write_text("## SEC-001 A\n\n正文一\n\n## SEC-001 B\n\n正文二\n", encoding="utf-8")
    with pytest.raises(ValueError, match="重复"):
        parse_standards_md(md.read_text(encoding="utf-8"))
