"""混合检索对照：稠密 / 词法 BM25 / RRF 融合，跑在同一份冻结快照上。

上一轮规模评测的结论是"956 条真实规范下纯稠密检索≈随机，0.45 绝对门限失效"。
这条脚本只回答一个问题：**换方法能不能救活**，而不是继续换 embedding。

只读评测，不动生产链路：`StandardsRetriever.retrieve()` 一行没改，服务行为不变。
与生产对齐的只有两处口径——doc 侧文本是 `标题\n正文`（同 build_index），
query 侧走 embed_query（指令前缀只加在稠密侧，词法支路不吃它）。

用法：
    HF_HUB_OFFLINE=1 uv run --extra embed python scripts/bench_hybrid_retrieval.py
产物：experiments/hybrid-retrieval-<model>.md + data/corpus/hybrid-<model>.json
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bench_retrieval_scale import CORPUS, KS, MANIFEST, REPO, SNAPSHOT, rule_id, slugify
from pr_reviewer.bm25 import Bm25Index, rrf
from pr_reviewer.config import Settings
from pr_reviewer.rag import _cosine, make_embedder, parse_standards_md

TOP_N = 20                 # 每路取前 20 参与融合与评测
RRF_K = 60                 # 原论文默认值；k 越小越看重头部名次
LEAK_TARGET = 0.10         # 工作点约束：干净块误放行 ≤ 10%
CACHE = CORPUS / "embedding-cache.json"


class EmbedCache:
    """向量缓存：换"方法"不该重付一遍 embedding 的钱。

    键含模型名——不同模型的向量不能混用，和项目里索引指纹防混库是同一条原则。
    """

    def __init__(self, model_name: str):
        self.model = model_name
        self.data: dict[str, dict[str, list[float]]] = (
            json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {})
        self.bucket = self.data.setdefault(model_name, {})
        self.misses = 0

    def get(self, texts: list[str], embed) -> list[list[float]]:
        todo = [t for t in dict.fromkeys(texts) if t not in self.bucket]
        if todo:
            self.misses += len(todo)
            for t, vec in zip(todo, embed(todo), strict=True):
                self.bucket[t] = vec
        return [self.bucket[t] for t in texts]

    def save(self) -> None:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")


def dense_rank(query_vecs: list[list[float]], doc_ids: list[str],
               doc_vecs: list[list[float]]) -> list[list[tuple[str, float]]]:
    """全量线性扫描算余弦：956 条规模下不需要 ANN，也更可复现。"""
    return [sorted(((rid, _cosine(qv, dv)) for rid, dv in zip(doc_ids, doc_vecs, strict=True)),
                   key=lambda t: (-t[1], t[0]))[:TOP_N] for qv in query_vecs]


def fused(dense_ids: list[list[str]], lexical_ids: list[list[str]]) -> list[tuple[str, float]]:
    """RRF 融合，并给出与 rrf() 同一套算式的分数，供后面反推工作点阈值。"""
    pos_d = {rid: i for i, rid in enumerate(dense_ids)}
    pos_b = {rid: i for i, rid in enumerate(lexical_ids)}
    merged = rrf([dense_ids, lexical_ids], k=RRF_K, top_n=TOP_N)
    return [(rid, round((1 / (RRF_K + pos_d[rid] + 1) if rid in pos_d else 0.0)
                        + (1 / (RRF_K + pos_b[rid] + 1) if rid in pos_b else 0.0), 6))
            for rid in merged]


def rank_metrics(ranked: list[list[str]], wanted: list[str]) -> dict:
    n = len(wanted)
    return {"recall@k": {str(k): round(sum(1 for r, w in zip(ranked, wanted, strict=True)
                                           if w in r[:k]) / n, 4) for k in KS},
            "mrr": round(statistics.fmean(1 / (r.index(w) + 1) if w in r else 0.0
                                          for r, w in zip(ranked, wanted, strict=True)), 4)}


def operating_point(pairs: list[list[tuple[str, float]]], wanted: list[str],
                    clean_top1: list[float], top_k: int = 10) -> dict:
    """不假设绝对门限：由**当轮干净块 top1 分布**反推阈值，看"误放行 ≤10%"时还剩多少召回。

    绝对阈值不可跨语料复用已经被证过（0.45 在 CWE 语料上形同不存在），
    所以这里每档每模式各自标定自己的门限，比较的是"同样守住 10% 泄漏，谁能召回更多"。
    """
    best: dict | None = None
    ordered = sorted(clean_top1)
    for name, q in (("p50", .5), ("p75", .75), ("p90", .9), ("p95", .95), ("p99", .99)):
        cut = ordered[min(len(ordered) - 1, int(len(ordered) * q))]
        leak = sum(1 for s in clean_top1 if s >= cut) / len(clean_top1)
        if leak > LEAK_TARGET:
            continue
        recall = sum(1 for lst, w in zip(pairs, wanted, strict=True)
                     if w in [rid for rid, s in lst[:top_k] if s >= cut]) / len(wanted)
        # 全为 0 时也要报出所选阈值：那本身就是结论（守住泄漏就没有召回可用）
        if best is None or (recall, -leak) > (best["recall"], -best["leak"]):
            best = {"recall": round(recall, 4), "quantile": name,
                    "threshold": round(cut, 6), "leak": round(leak, 4)}
    return best or {"recall": 0.0, "quantile": None, "threshold": None, "leak": None,
                    "note": "没有任何阈值能把泄漏压到目标以下"}


def main() -> int:
    ap = argparse.ArgumentParser(description="混合检索对照")
    ap.add_argument("--tiers", default="", help="只跑这些档位名（逗号分隔）")
    ap.add_argument("--limit", type=int, default=0, help="每档最多跑多少查询（冒烟用）")
    args = ap.parse_args()

    if not SNAPSHOT.exists():
        print("先跑一次 bench_retrieval_scale.py 生成查询快照", file=sys.stderr)
        return 2
    snap = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    queries, clean = snap["queries"], snap["clean"]
    if args.limit:
        queries, clean = queries[: args.limit], clean[: max(1, args.limit // 2)]
        print(f"!! 冒烟模式：{len(queries)} 查询 / {len(clean)} 干净块，数字不代表结论")
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    settings = Settings(_env_file=None)
    embedder = make_embedder(settings)
    print(f"稠密模型：{embedder.name}（词法与融合支路不依赖它）")
    cache = EmbedCache(embedder.name)

    def embed_docs(texts: list[str]) -> list[list[float]]:
        return embedder.embed(texts)

    def embed_queries(texts: list[str]) -> list[list[float]]:
        """`embed_query` 的签名是**单条文本**（指令前缀逐条加在 query 侧）。

        冒烟测试就是在这里炸的：整批列表直接交给它会返回多条向量，
        `zip(..., strict=True)` 当场报 "argument 2 is longer than argument 1"。
        """
        return [embedder.embed_query(t) for t in texts]
    wanted = [rule_id(q["cwe"]) for q in queries]

    results: dict[str, dict] = {}
    for tier_name, tier in manifest["tiers"].items():
        if args.tiers and tier_name not in args.tiers.split(","):
            continue
        rules = [(r["rule_id"], f"{r['title']}\n{r['text']}")
                 for r in parse_standards_md((CORPUS / tier["file"]).read_text(encoding="utf-8"))]
        ids = [rid for rid, _ in rules]
        bm = Bm25Index()
        for rid, text in rules:
            bm.add(rid, text)

        doc_vecs = cache.get([t for _, t in rules], embed_docs)
        q_vecs = cache.get([q["chunk"]["content"] for q in queries], embed_queries)
        c_vecs = cache.get([c["chunk"]["content"] for c in clean], embed_queries)

        d_q = dense_rank(q_vecs, ids, doc_vecs)
        d_c = dense_rank(c_vecs, ids, doc_vecs)
        b_q = [bm.search(q["chunk"]["content"], TOP_N) for q in queries]
        b_c = [bm.search(c["chunk"]["content"], TOP_N) for c in clean]
        h_q = [fused([rid for rid, _ in d], [rid for rid, _ in b]) for d, b in zip(d_q, b_q,
                                                                                  strict=True)]
        h_c = [fused([rid for rid, _ in d], [rid for rid, _ in b]) for d, b in zip(d_c, b_c,
                                                                                  strict=True)]

        modes = {}
        for name, pairs, clean_pairs in (("稠密", d_q, d_c), ("词法", b_q, b_c),
                                         ("混合RRF", h_q, h_c)):
            ranked = [[rid for rid, _ in lst] for lst in pairs]
            modes[name] = {**rank_metrics(ranked, wanted),
                           "工作点": operating_point(pairs, wanted,
                                                    [lst[0][1] if lst else 0.0
                                                     for lst in clean_pairs])}
        results[tier_name] = {"rules": tier["rule_count"],
                              "with_code": tier["rules_with_official_code"], "modes": modes}
        print(f"  {tier['rule_count']:>4} 条（带官方示例 {tier['rules_with_official_code']:>3}）"
              + "".join(f" | {m}@5={modes[m]['recall@k']['5']:.2f}"
                        f"@20={modes[m]['recall@k']['20']:.2f}" for m in modes))

    cache.save()
    slug = slugify(embedder.name)
    payload = {"embedder": embedder.name, "rrf_k": RRF_K, "leak_target": LEAK_TARGET,
               "snapshot": snap["stats"], "top_n": TOP_N, "results": results}
    out_json = CORPUS / f"hybrid-{slug}.json"
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    report = REPO / "experiments" / f"hybrid-retrieval-{slug}.md"
    write_report(payload, report)
    print(f"\n新算 embedding {cache.misses} 条（已缓存到 {CACHE}）\n→ {report}")
    return 0


def write_report(payload: dict, path: Path) -> None:
    hdr = ["语料条数", "带官方示例", "模式", "recall@1", "recall@5", "recall@10",
           "recall@20", "MRR", "守10%泄漏时recall@10", "该工作点阈值", "实际泄漏"]
    s = payload["snapshot"]
    lines = ["# 混合检索对照（稠密 / BM25 词法 / RRF 融合）", "",
             f"- 稠密模型：{payload['embedder']}；RRF k={payload['rrf_k']}；"
             f"每路取前 {payload['top_n']}",
             f"- 数据：同一份冻结快照——{s['queries']} 个真实代码块（标注来自 Bandit）"
             f"+ {s['clean']} 个干净对照块；种子与档位见 retrieval-scale-*.md",
             f"- 工作点口径：不假设绝对门限，由**当轮干净块 top1 分布**反推阈值，"
             f"比较「误放行 ≤ {payload['leak_target']:.0%} 时还能召回多少」",
             "", "| " + " | ".join(hdr) + " |", "|" + "---|" * len(hdr)]
    for tier in sorted(payload["results"].values(), key=lambda v: v["rules"]):
        for mode in ("稠密", "词法", "混合RRF"):
            m, op = tier["modes"][mode], tier["modes"][mode]["工作点"]
            lines.append(f"| {tier['rules']} | {tier['with_code']} | {mode} "
                         f"| {m['recall@k']['1']:.2f} | {m['recall@k']['5']:.2f} "
                         f"| {m['recall@k']['10']:.2f} | {m['recall@k']['20']:.2f} "
                         f"| {m['mrr']:.2f} | {op['recall']:.2f} | {op['threshold']} "
                         f"| {op['leak']} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
