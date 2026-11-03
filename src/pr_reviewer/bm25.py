"""词法检索（BM25）——给"代码块检索规范条款"准备的另一条支路。

为什么单独一个模块而不直接接进 `rag.py` 的 `retrieve()`：这一版只做只读评测，
先用同一份冻结快照证明"混合检索能不能把 956 条规模下的召回从随机水平抬起来"，
再决定要不要动生产链路。评测脚本直接 import 这里，服务的行为一行不变。

为什么值得试词法：代码块的判别信号大量落在**字面标识符**上（`sqlite3.execute`、
`subprocess(shell=True)`、`md5(`、`verify=False`），而稠密向量把它们和 surrounding
语法一起平均掉了；CWE 那种散文条款里恰好也写着这些名字。词法支路保住的正是这部分。

分词不依赖 jieba：英文/数字按标识符切（含 camelCase / snake_case 拆子词），
CJK 同时产出单字与相邻二字组合——没有词典时这是对中文最不误伤的做法，
代价是查询侧的"为这个句子…"这类指令前缀会贡献一些无信息量 token，
所以那类前缀只在稠密侧加，词法侧不吃它。
"""
from __future__ import annotations

import math
import re
from collections import Counter

# 驼峰与数字边界都要保住：HTTPServer → http + server，sqlite3execute → sqlite + 3 + execute。
# 必须在**原文**上匹配：先 lower 就把大小写边界丢了，只剩一个整词。
# 四段各有分工，缺一类就漏词（实测踩过：只写前三段时 `API_KEY` 与 `MD5` 会被整段丢掉，
# 而 `API_KEY` 恰恰是硬编码密钥条款最该命中的词面信号）：
#   1) 缩写前缀（HTTPServer 里的 HTTP，后面紧跟大写+小写）
#   2) 普通单词（可带一个首大写）
#   3) 数字串
#   4) 纯大写串兜底（API / KEY / MD5）
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[0-9]+|[A-Z]+")
_CJK = re.compile(r"[\u4e00-\u9fff]")


def tokenize(text: str) -> list[str]:
    """规范条款与代码块共用同一套切法，否则两侧词表对不上。"""
    tokens = [t.lower() for t in _CAMEL.findall(text)]
    cjk = _CJK.findall(text)
    for i, ch in enumerate(cjk):
        tokens.append(ch)
        if i + 1 < len(cjk):
            tokens.append(ch + cjk[i + 1])
    return tokens


class Bm25Index:
    """Okapi BM25。k1 控制词频饱和，b 控制文档长度归一化强度。"""

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self._postings: dict[str, list[tuple[int, int]]] = {}
        self._lengths: list[int] = []
        self._ids: list[str] = []

    def __len__(self) -> int:
        return len(self._ids)

    def add(self, doc_id: str, text: str) -> None:
        idx = len(self._ids)
        self._ids.append(doc_id)
        counts = Counter(tokenize(text))
        self._lengths.append(sum(counts.values()))
        for token, tf in counts.items():
            self._postings.setdefault(token, []).append((idx, tf))

    def search(self, query: str, top_k: int = 10) -> list[tuple[str, float]]:
        """返回 [(doc_id, score)]，分数降序；同分时按入库顺序，保证可复现。"""
        n = len(self._ids)
        if not n:
            return []
        avgdl = sum(self._lengths) / n
        # 用普通 dict 而不是 Counter 累加：Counter 的值类型是 int，会把 BM25 的浮点分数截断
        scores: dict[int, float] = {}
        for token in set(tokenize(query)):
            postings = self._postings.get(token)
            if not postings:
                continue
            df = len(postings)
            idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
            for idx, tf in postings:
                norm = self.k1 * (1 - self.b + self.b * self._lengths[idx] / avgdl)
                scores[idx] = scores.get(idx, 0.0) + idf * (tf * (self.k1 + 1)) / (tf + norm)
        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
        return [(self._ids[i], round(s, 6)) for i, s in ranked[:top_k]]


def rrf(rankings: list[list[str]], k: int = 60, top_n: int = 10) -> list[str]:
    """Reciprocal Rank Fusion：只用名次不用分数，天然免掉"两路分数不可比"的问题。

    score(d) = Σ_路 1 / (k + rank)。k 取 60 是原论文的默认值，作用是压掉头部名次的
    过度权重；这里 k 可配，方便以后扫参。
    """
    totals: dict[str, float] = {}
    for ranking in rankings:
        for position, doc_id in enumerate(ranking, start=1):
            totals[doc_id] = totals.get(doc_id, 0.0) + 1.0 / (k + position)
    ordered = sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))
    return [doc_id for doc_id, _ in ordered[:top_n]]
