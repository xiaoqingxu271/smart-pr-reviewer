"""需求 1.3：规范检索 RAG（PRD §5.3）。

三层解耦，便于离线测试：
- parse_standards_md(): 纯逻辑，按 "## SEC-xxx 标题" 条款切块，rule_id 为元数据
- EmbeddingBackend:    local（sentence-transformers，默认）/ ollama / openai /
                       hash（离线测试用确定性词袋；缺依赖时自动降级到它）
- VectorStore:         ChromaDB 优先（PRD 指定），未安装时降级 JSON 内存-磁盘存储

语料规模是内部规范文档（几十条），检索采用全量余弦相似度，避免 Chroma 距离度量
口径差异。索引指纹（INDEX_META_FILE）同时记录规范文件 sha256 与 embedding 模型名
+维度：换模型必然换向量空间，指纹不一致即重建，杜绝新旧向量混库。

过滤判据（实证选型见 experiments/rag-embedding-bench.md）：门限只判 top1，
margin 只裁尾部。因为实测"正确条款恒为 top1"，所以漏召只会漏成空结果
（prompt 声明无匹配、依通用常识审查），不会把无关条款当规范依据。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
import warnings
from pathlib import Path
from typing import Protocol

from pr_reviewer.config import Settings
from pr_reviewer.models import RuleHit

RULE_HEADER = re.compile(r"^##\s+(SEC-\d{3,})\s+(.*)$")
_WORD = re.compile(r"[a-zA-Z0-9_]+")
_FENCE = re.compile(r"^\s*(?:```|~~~)")

INDEX_META_FILE = ".standards.index.json"
LEGACY_HASH_FILE = ".standards.sha256"      # 旧版只存 sha256 的侧文件，迁移后删除


# ---------------------------------------------------------------- 条款解析

def parse_standards_md(text: str) -> list[dict]:
    """按二级标题切块；标题必须含条款编号（如 "## SEC-002 SQL 查询禁止字符串拼接"）。

    一级标题与首个条款前的说明文字不入库。代码围栏（``` / ~~~）内的行一律不参与
    标题判定：条款示例里的 Python 注释行以 `#` 开头，若当成块边界会把条款正文静默
    截断——规范文档带代码示例后这是必然踩到的。
    """
    rules: list[dict] = []
    current: dict | None = None
    in_fence = False

    def close() -> None:
        nonlocal current
        if current:
            current["text"] = current["text"].strip()
            rules.append(current)
        current = None

    for line in text.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
            if current is not None:
                current["text"] += line + "\n"
            continue
        m = RULE_HEADER.match(line)
        if m and not in_fence:
            close()
            current = {"rule_id": m.group(1), "title": m.group(2).strip(), "text": ""}
        elif line.startswith("#") and not in_fence:
            close()               # 更深层级标题视为条款内容的边界
        elif current is not None:
            current["text"] += line + "\n"
    close()
    return [r for r in rules if r["text"]]


# ---------------------------------------------------------------- Embedding

class EmbeddingUnavailable(RuntimeError):
    """要求的 embedding 后端在当前环境不可用。"""


class EmbeddingBackend(Protocol):
    """doc 侧用 embed()，query 侧用 embed_query()——非对称检索指令只作用于 query。"""

    name: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


def _with_prefix(text: str, prefix: str) -> str:
    return f"{prefix}{text}" if prefix else text


class HashEmbedder:
    """离线/测试用确定性 embedding：拉丁词 + CJK 二元组词袋哈希。

    过滤纯数字与短 token（<3 字符），减少编号类无信号 token 稀释相似度。
    它没有语义能力，跨语言（中文条款 vs 英文代码）时分数天然偏低：条款正文无代码
    示例时实测 top1 均分仅 0.137，补示例后升到 0.286——但绝对分数仍与真实模型
    不是一个口径，用它必须另行标定 RETRIEVAL_SCORE_THRESHOLD。
    仅作测试替身与缺依赖时的降级，不建议用于生产。
    """

    def __init__(self, dim: int = 256):
        self.dim = dim
        self.name = f"hash-bow-{dim}d"
        self.query_prefix = ""

    @staticmethod
    def _tokens(text: str) -> list[str]:
        toks = [w for w in _WORD.findall(text.lower()) if not w.isdigit() and len(w) >= 3]
        for run in re.findall(r"[\u4e00-\u9fff]+", text):
            toks.extend(run[i:i + 2] for i in range(len(run) - 1))
        return toks

    def _one(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        for tok in self._tokens(text):
            idx = int(hashlib.md5(tok.encode()).hexdigest(), 16) % self.dim
            v[idx] += 1.0
        return v

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._one(_with_prefix(text, self.query_prefix))


class LocalEmbedder:
    """sentence-transformers 本地模型：源码不出机器，CPU 可跑（默认 bge-base-zh-v1.5）。

    输出做 L2 归一化，因此 cosine / 内积 / L2 三种口径等价，检索侧的余弦分数
    与 Chroma 的 `hnsw:space` 设置脱钩。
    """

    def __init__(self, model_name: str, query_prefix: str = ""):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise EmbeddingUnavailable(
                "embedding_backend=local 需要 sentence-transformers，"
                "请安装：uv sync --extra embed（或改用 embedding_backend=ollama/openai）"
            ) from exc
        self._model = SentenceTransformer(model_name)
        self.name = f"st:{model_name}"
        self.query_prefix = query_prefix

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self._model.encode(texts, normalize_embeddings=True,
                                  show_progress_bar=False).tolist()

    def embed_query(self, text: str) -> list[float]:
        return self.embed([_with_prefix(text, self.query_prefix)])[0]


class OllamaEmbedder:
    """本地 Ollama embedding（隐私模式），走 HTTP API，中文友好（如 bge-m3）。"""

    def __init__(self, base_url: str, model: str, query_prefix: str = ""):
        self.base_url, self.model = base_url.rstrip("/"), model
        self.name = f"ollama:{model}"
        self.query_prefix = query_prefix

    def embed(self, texts: list[str]) -> list[list[float]]:
        import httpx
        resp = httpx.post(f"{self.base_url}/api/embed",
                          json={"model": self.model, "input": texts}, timeout=60)
        resp.raise_for_status()
        return resp.json()["embeddings"]

    def embed_query(self, text: str) -> list[float]:
        return self.embed([_with_prefix(text, self.query_prefix)])[0]


class OpenAIEmbedder:
    def __init__(self, api_key: str, model: str, base_url: str = "",
                 query_prefix: str = ""):
        self.model, self.base_url, self.api_key = model, base_url, api_key
        self.name = f"openai:{model}"
        self.query_prefix = query_prefix
        self._client = None

    def _ensure_client(self):
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI(api_key=self.api_key, base_url=self.base_url or None)
        return self._client

    def embed(self, texts: list[str]) -> list[list[float]]:
        client = self._ensure_client()
        resp = client.embeddings.create(model=self.model, input=texts)
        return [d.embedding for d in resp.data]

    def embed_query(self, text: str) -> list[float]:
        return self.embed([_with_prefix(text, self.query_prefix)])[0]


def make_embedder(settings: Settings) -> EmbeddingBackend:
    backend, prefix = settings.embedding_backend, settings.embedding_query_prefix
    if backend == "hash":
        return HashEmbedder()
    try:
        if backend == "openai":
            return OpenAIEmbedder(settings.openai_api_key, settings.embedding_model,
                                  settings.openai_base_url, query_prefix=prefix)
        if backend == "local":
            return LocalEmbedder(settings.embedding_model, query_prefix=prefix)
        return OllamaEmbedder(settings.ollama_base_url, settings.embedding_model,
                              query_prefix=prefix)
    except EmbeddingUnavailable as exc:
        # 处处可降级：缺依赖时退回 hash 词袋保证链路可用，但必须显式告警——
        # 检索质量与阈值标定都随之改变（报告 / health 里的 rag 状态可见）。
        warnings.warn(f"{exc}；本次降级为 hash 词袋检索（分数口径不同，"
                      f"需重标定 RETRIEVAL_SCORE_THRESHOLD）",
                      RuntimeWarning, stacklevel=2)
        return HashEmbedder()


# ---------------------------------------------------------------- 向量存储

class VectorStore(Protocol):
    def clear(self) -> None: ...
    def add(self, ids: list[str], embeddings: list[list[float]],
            documents: list[str], metadatas: list[dict]) -> None: ...
    def all(self) -> list[dict]:  # [{"id", "embedding", "document", "metadata"}]
        ...


class InMemoryStore:
    """JSON 持久化的小语料存储；chromadb 不可用时的降级实现（PRD §6.3）。"""

    def __init__(self, path: Path):
        self.path = path
        self._items: list[dict] = []
        if path.exists():
            self._items = json.loads(path.read_text(encoding="utf-8"))

    def _flush(self) -> None:
        self.path.write_text(json.dumps(self._items, ensure_ascii=False), encoding="utf-8")

    def clear(self) -> None:
        self._items = []
        self._flush()

    def add(self, ids, embeddings, documents, metadatas) -> None:
        self._items.extend({"id": i, "embedding": e, "document": d, "metadata": m}
                           for i, e, d, m in zip(ids, embeddings, documents, metadatas))
        self._flush()

    def all(self) -> list[dict]:
        return self._items


class ChromaStore:
    """ChromaDB 持久化存储（PRD 指定），collection=coding_standards。"""

    def __init__(self, persist_dir: Path):
        import chromadb  # 延迟导入
        self._client = chromadb.PersistentClient(path=str(persist_dir))
        self._col = self._client.get_or_create_collection("coding_standards")

    def clear(self) -> None:
        if self._col.count():
            self._client.delete_collection("coding_standards")
            self._col = self._client.get_or_create_collection("coding_standards")

    def add(self, ids, embeddings, documents, metadatas) -> None:
        self._col.add(ids=ids, embeddings=embeddings,
                      documents=documents, metadatas=metadatas)

    def all(self) -> list[dict]:
        got = self._col.get(include=["documents", "metadatas", "embeddings"])
        return [{"id": i, "embedding": e, "document": d, "metadata": m}
                for i, e, d, m in zip(got["ids"], got["embeddings"],
                                      got["documents"], got["metadatas"])]


def make_store(settings: Settings) -> VectorStore:
    try:
        return ChromaStore(settings.chroma_dir)
    except ImportError:
        warnings.warn("chromadb 未安装，规范库降级为 JSON 存储（生产环境请安装 chromadb）",
                      RuntimeWarning, stacklevel=2)
        return InMemoryStore(settings.chroma_dir / "standards.json")


# ---------------------------------------------------------------- 检索器

def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


class StandardsRetriever:
    def __init__(self, settings: Settings,
                 store: VectorStore | None = None, embedder: EmbeddingBackend | None = None):
        self.settings = settings
        self.store = store or make_store(settings)
        self.embedder = embedder or make_embedder(settings)
        self._items: list[dict] | None = None

    # -- 索引 ------------------------------------------------------------

    def _meta_path(self) -> Path:
        return self.settings.chroma_dir / INDEX_META_FILE

    def _read_meta(self) -> dict:
        """索引指纹：规范文件 hash + embedding 模型名 + 向量维度。"""
        try:
            return json.loads(self._meta_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def build_index(self, md_path: str | Path | None = None) -> int:
        """解析规范文档并按需重建索引（PRD §5.3 更新机制）。

        指纹任一字段不符即重建：换 embedding 模型等于换向量空间，新旧向量混在
        同一个 collection 里会让检索分数不可解释，维度不同时还会直接报错。
        返回入库条款数。
        """
        path = Path(md_path or self.settings.standards_md_path)
        text = path.read_text(encoding="utf-8")
        digest = hashlib.sha256(text.encode()).hexdigest()
        self.settings.chroma_dir.mkdir(parents=True, exist_ok=True)

        probe = self.store.all()
        meta = self._read_meta()
        stored_dim = len(probe[0]["embedding"]) if probe else 0
        if (probe and meta.get("standards_sha256") == digest
                and meta.get("embedder") == self.embedder.name
                and meta.get("dim") == stored_dim):
            self._items = None  # 让后续 retrieve 重新从 store 加载
            return len({item["id"] for item in probe})

        rules = parse_standards_md(text)
        self.store.clear()
        embedded: list[list[float]] = []
        if rules:
            # 嵌入 title+text（不含条款编号前缀，编号本身无语义且稀释向量）
            embedded = self.embedder.embed([f"{r['title']}\n{r['text']}" for r in rules])
            self.store.add(
                ids=[r["rule_id"] for r in rules],
                embeddings=embedded,
                documents=[r["text"] for r in rules],
                metadatas=[{"rule_id": r["rule_id"], "title": r["title"],
                             "embedder": self.embedder.name} for r in rules],
            )
        self._meta_path().write_text(json.dumps({
            "standards_sha256": digest,
            "embedder": self.embedder.name,
            "dim": len(embedded[0]) if embedded else 0,
            "rule_count": len(rules),
            "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        (self.settings.chroma_dir / LEGACY_HASH_FILE).unlink(missing_ok=True)
        self._items = None
        return len(rules)

    # -- 检索 ------------------------------------------------------------

    def _load(self) -> list[dict]:
        if self._items is None:
            self._items = self.store.all()
        return self._items

    def size(self) -> int:
        """规范库条款数（报告与日志的仪表字段）。"""
        return len(self._load())

    def retrieve(self, query: str, top_k: int | None = None) -> list[RuleHit]:
        """top1 门限 + margin 裁尾；top1 不够高时返回 []（PRD §5.3 空结果行为）。

        判据来自 experiments/rag-embedding-bench.md：绝对分数阈值切不开
        「正确条款」与「干净代码」两个重叠的分数带；而实测正确条款恒为 top1，
        所以把门限只压在 top1 上，漏召的表现就是「无匹配条款」（prompt 声明依
        通用常识审查），不会表现为「依据了错误条款」。
        """
        items = self._load()
        if not items:
            return []
        qvec = self.embedder.embed_query(query)
        threshold = self.settings.retrieval_score_threshold
        k = top_k or self.settings.retrieval_top_k
        scored = sorted(
            ((item["metadata"]["rule_id"], item["document"],
              _cosine(qvec, item["embedding"])) for item in items),
            key=lambda t: -t[2],
        )[:k]
        if not scored or scored[0][2] < threshold:
            return []
        floor = max(threshold, scored[0][2] - self.settings.retrieval_margin)
        return [RuleHit(rule_id=rid, text=doc, score=round(score, 4))
                for rid, doc, score in scored if score >= floor]

    def has_rule(self, rule_id: str) -> bool:
        """幻觉后校验用（PRD §5.4）：确认 issue 引用的条款号真实存在。"""
        return any(item["id"] == rule_id for item in self._load())

    def rule_text(self, rule_id: str) -> str | None:
        """报告用：取条款原文摘录。"""
        for item in self._load():
            if item["id"] == rule_id:
                return item["document"]
        return None
