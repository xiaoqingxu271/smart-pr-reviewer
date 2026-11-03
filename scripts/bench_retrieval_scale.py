"""检索层随语料规模的退化曲线：真实 CWE 语料 + Bandit 在真实代码上给的标注。

要回答的问题：现在 0.45 门限 / top_k=5 是在 **17 条**条款上标定的，语料涨到 100、956 条
（真实团队规范就是这个量级）时，同一批查询还能不能召回正确条款？门限该重新标到哪？

数据全部来自第三方，脚本不产生任何规则文本或标注：
- 语料：`scripts/build_cwe_corpus.py` 产出的 CWE 官方条目（三档嵌套）
- 查询与标注：`scripts/label_with_bandit.py` 在你真实代码上跑出的 (代码块 → CWE 编号)
- 干净对照组：同一批文件里**没有被 Bandit 标记过**的代码块，用来量"误放行"

用法：
    uv run --extra embed python scripts/bench_retrieval_scale.py
产物：experiments/retrieval-scale.md（人看）+ data/corpus/scale-results.json（含结果哈希，
     连跑两次哈希必须一致，否则说明有未固定的随机性）
"""
from __future__ import annotations

import hashlib
import json
import random
import statistics
import sys
import tempfile
from pathlib import Path

from pr_reviewer.chunker import chunk_file
from pr_reviewer.config import Settings
from pr_reviewer.rag import InMemoryStore, StandardsRetriever, make_embedder

REPO = Path(__file__).resolve().parent.parent
CORPUS = REPO / "data" / "corpus"
MANIFEST = CORPUS / "manifest.json"
LABELS = REPO / "data" / "labels" / "bandit_findings.json"
REPORT = REPO / "experiments" / "retrieval-scale.md"

MAX_PER_RULE = 20          # 每种 CWE 最多取多少查询：不让 CWE-703 的几百条淹没稀有项
N_CLEAN = 120              # 干净对照组样本量
SEED = 20261102
KS = (1, 3, 5, 10, 20)


def rule_id(cwe: int) -> str:
    return f"CWE-{cwe:04d}"


def build_queries() -> tuple[list[dict], list[dict], dict]:
    """(漏洞查询, 干净对照, 统计) —— 查询文本用生产分块器的语法块。"""
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    labels = json.loads(LABELS.read_text(encoding="utf-8"))
    dropped_tests = set(manifest["labels"]["dropped_test_ids"])
    # manifest 的键是 JSON 字符串，findings 里是 int：不转就漏滤，
    # 那 18 条"期望条款已被官方废弃"的查询会冒充漏召，把 recall 压低
    unrecallable = {int(k) for k in manifest["unrecallable_cwes"]}

    per_cwe: dict[int, list[dict]] = {}
    seen: set[tuple[str, int]] = set()
    cache: dict[str, list[dict]] = {}
    unreadable = 0
    for f in sorted(labels["findings"], key=lambda x: (x["cwe_id"] or 0, x["filename"],
                                                       x["line_number"])):
        cwe, path = f.get("cwe_id"), f["filename"]
        if not cwe or f["test_id"] in dropped_tests or cwe in unrecallable:
            continue
        if path not in cache:
            try:
                content = Path(path).read_text(encoding="utf-8", errors="replace")
                cache[path] = [c.model_dump() for c in
                               chunk_file(path, content, list(range(1, 10**6)))]
            except OSError:
                cache[path] = []
                unreadable += 1
        hit = next((c for c in cache[path]
                    if c["start_line"] <= f["line_number"] <= c["end_line"]), None)
        if hit is None or (path, hit["start_line"]) in seen:
            continue
        seen.add((path, hit["start_line"]))
        per_cwe.setdefault(cwe, []).append({"cwe": cwe, "file": path,
                                            "line": f["line_number"],
                                            "test_id": f["test_id"], "chunk": hit})

    queries = []
    for _cwe, items in sorted(per_cwe.items()):
        queries.extend(items[:MAX_PER_RULE])

    # 干净对照：同一批文件里没有任何 finding 的语法块（同分布、不同标签）
    flagged = {(q["file"], q["chunk"]["start_line"]) for q in queries}
    rng = random.Random(SEED)
    pool = []
    for path, chunks in cache.items():
        for c in chunks:
            if c["content"].strip() and (path, c["start_line"]) not in flagged:
                pool.append({"file": path, "chunk": c})
    rng.shuffle(pool)
    clean = pool[:N_CLEAN]
    stats = {"cwe_kinds": len(per_cwe), "queries": len(queries), "clean": len(clean),
             "files": len(cache), "unreadable_files": unreadable,
             "per_cwe_all": {str(k): len(v) for k, v in sorted(per_cwe.items())}}
    return queries, clean, stats


def measure(retriever: StandardsRetriever, queries: list[dict], threshold: float,
            top_k: int) -> dict:
    """在给定门限下跑一遍查询：召回、分数带、注入条数。"""
    s = retriever.settings
    s.retrieval_score_threshold, s.retrieval_margin = threshold, 1.0
    s.retrieval_top_k = top_k
    ranks, hits_len, correct_scores, kept = [], [], [], 0
    for q in queries:
        want = rule_id(q["cwe"])
        hits = retriever.retrieve(q["chunk"]["content"])      # 每查询只检索一次
        ranked = [h.rule_id for h in hits]
        rank = ranked.index(want) + 1 if want in ranked else 99
        ranks.append(rank)
        hits_len.append(len(ranked))
        correct_scores.append(hits[0].score if ranked and ranked[0] == want else 0.0)
        if rank <= top_k:
            kept += 1
    n = len(queries)
    return {"recall@k": {str(k): round(sum(1 for r in ranks if r <= k) / n, 4) for k in KS},
            "mrr": round(statistics.fmean(1 / r if r < 99 else 0 for r in ranks), 4),
            "正确条款进前k比例": round(kept / n, 4),
            "正确条款top1分数": {"min": round(min(correct_scores), 4),
                                 "median": round(statistics.median(correct_scores), 4)},
            "平均返回条款数": round(statistics.fmean(hits_len), 2)}


def measure_clean(retriever: StandardsRetriever, clean: list[dict], threshold: float,
                  top_k: int) -> dict:
    """干净块：门限应当把它们拦住（返回空）。放行 = 无问题代码被塞进规范条款。"""
    s = retriever.settings
    s.retrieval_score_threshold, s.retrieval_margin = threshold, s.retrieval_margin
    s.retrieval_top_k = top_k
    top1_scores, leaked, injected = [], 0, []
    for c in clean:
        hits = retriever.retrieve(c["chunk"]["content"])
        top1_scores.append(hits[0].score if hits else 0.0)
        if hits:
            leaked += 1
            injected.append(len(hits))
    return {"误放行率": round(leaked / len(clean), 4),
            "误放行块平均注入条款数": round(statistics.fmean(injected), 2) if injected else 0,
            "干扰项top1分数": {"median": round(statistics.median(top1_scores), 4),
                               "p90": round(sorted(top1_scores)[int(len(top1_scores) * 0.9)], 4),
                               "max": round(max(top1_scores), 4)}}


def main() -> int:
    if not MANIFEST.exists():
        print("先跑 scripts/build_cwe_corpus.py", file=sys.stderr)
        return 2
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    queries, clean, qstats = build_queries()
    print(f"查询 {qstats['queries']} 条（{qstats['cwe_kinds']} 个 CWE，每种上限 "
          f"{MAX_PER_RULE}）；干净对照 {qstats['clean']} 块；来源文件 {qstats['files']} 个")

    settings0 = Settings(_env_file=None)
    print("加载 embedding 模型（真实 bge，CPU）…")
    embedder = make_embedder(settings0)
    print(f"  embedder = {embedder.name}")

    base = Settings(_env_file=None, embedding_backend=settings0.embedding_backend,
                    retrieval_top_k=5, retrieval_score_threshold=0.45, retrieval_margin=0.05)
    results: dict[str, dict] = {}
    for tier_name, tier in manifest["tiers"].items():
        md = CORPUS / tier["file"]
        with tempfile.TemporaryDirectory() as tmp:
            s = Settings(_env_file=None, standards_md_path=md,
                         embedding_backend=base.embedding_backend,
                         retrieval_top_k=base.retrieval_top_k,
                         retrieval_score_threshold=base.retrieval_score_threshold,
                         retrieval_margin=base.retrieval_margin)
            r = StandardsRetriever(s, store=InMemoryStore(Path(tmp) / "s.json"),
                                   embedder=embedder)
            built = r.build_index(md)
            # 自诊断：这一档必须包含全部期望条款，否则 recall 不是"干扰变多"的影响，数字不可比
            want_ids = {rule_id(q["cwe"]) for q in queries}
            absent = sorted(want_ids - {item["id"] for item in r._load()})
            if absent:
                raise SystemExit(f"!! 档位 {tier_name} 缺期望条款 {absent}，先修语料再测")
            raw = measure(r, queries, threshold=0.0, top_k=max(KS))     # 无门限：纯排序质量
            gated = measure(r, queries, threshold=base.retrieval_score_threshold,
                            top_k=base.retrieval_top_k)
            clean_m = measure_clean(r, clean, base.retrieval_score_threshold,
                                    base.retrieval_top_k)
        results[tier_name] = {"rules": built,
                              "rules_with_official_code": tier["rules_with_official_code"],
                              "无门限排序": raw,
                              f"生产口径@{base.retrieval_score_threshold}": gated,
                              "干净块": clean_m}
        print(f"  {built:>4} 条规则 | recall@1={raw['recall@k']['1']:.2f} "
              f"recall@5={raw['recall@k']['5']:.2f} recall@20={raw['recall@k']['20']:.2f} "
              f"| 门限0.45 保留={gated['正确条款进前k比例']:.2f} "
              f"| 干净块误放行={clean_m['误放行率']:.2f}")

    payload = {"manifest": {k: manifest[k] for k in
                            ("source_url", "catalog_version", "catalog_date", "zip_sha256",
                             "seed", "unrecallable_cwes", "labels")},
               "queries": qstats, "params": {"threshold": base.retrieval_score_threshold,
                                             "top_k": base.retrieval_top_k,
                                             "margin": base.retrieval_margin,
                                             "embedder": embedder.name},
               "results": results}
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    payload["result_sha256"] = hashlib.sha256(blob.encode()).hexdigest()[:16]
    (CORPUS / "scale-results.json").write_text(json.dumps(payload, ensure_ascii=False,
                                                          indent=1), encoding="utf-8")
    write_report(payload)
    print(f"\n结果哈希 {payload['result_sha256']}（连跑两次必须一致）→ {REPORT}")
    return 0


def write_report(payload: dict) -> None:
    m, res = payload["manifest"], payload["results"]
    lines = [
        "# 检索层随语料规模的退化（真实 CWE 语料 + Bandit 真实标注）",
        "",
        f"- 规则来源：{m['source_url']}（CWE {m['catalog_version']}，{m['catalog_date']}，"
        f"zip sha256 `{m['zip_sha256'][:16]}`）",
        f"- 标注来源：Bandit {m['labels']['bandit_version']} 扫描 "
        f"{m['labels']['files_scanned']} 个真实源码文件（{len(m['labels']['roots'])} 个目录），"
        f"取 {m['labels']['findings_used']} 条 finding 的 CWE 编号",
        f"- 剔除：`{m['labels']['dropped_test_ids']}`（Bandit 自己就建议在测试码里忽略）"
        f"共 {m['labels']['findings_dropped_by_test_id']} 条；"
        f"期望条款已被官方废弃而不可召回 {sum(m['unrecallable_cwes'].values())} 条",
        f"- 查询：{payload['queries']['queries']} 个语法块（{payload['queries']['cwe_kinds']} 个 "
        f"CWE，每种上限 20 条）；干净对照 {payload['queries']['clean']} 块；种子 {m['seed']}",
        f"- 参数：embedder={payload['params']['embedder']}，"
        f"门限={payload['params']['threshold']}，top_k={payload['params']['top_k']}，"
        f"margin={payload['params']['margin']}",
        f"- 结果哈希：`{payload['result_sha256']}`（同机重跑应完全一致）",
        "",
        "| 语料条数 | 带官方示例 | recall@1 | recall@3 | recall@5 | recall@10 | recall@20 "
        "| MRR | 门限0.45保留 | 干净块误放行 | 干扰项top1 median/p90 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    gated_key = f"生产口径@{payload['params']['threshold']}"
    for r in sorted(res.values(), key=lambda v: v["rules"]):
        raw, gated, cl = r["无门限排序"], r[gated_key]["正确条款进前k比例"], r["干净块"]
        lines.append(f"| {r['rules']} | {r['rules_with_official_code']} "
                     f"| {raw['recall@k']['1']:.2f} | {raw['recall@k']['3']:.2f} "
                     f"| {raw['recall@k']['5']:.2f} | {raw['recall@k']['10']:.2f} "
                     f"| {raw['recall@k']['20']:.2f} | {raw['mrr']:.2f} | {gated:.2f} "
                     f"| {cl['误放行率']:.2f} | {cl['干扰项top1分数']['median']:.3f}"
                     f" / {cl['干扰项top1分数']['p90']:.3f} |")
    lines += ["", "重建（`data/` 不入库：CWE 原始包 2 MB、标注里带本机绝对路径）：", "",
              "```bash",
              "curl -L -o data/raw/cwec_latest.xml.zip https://cwe.mitre.org/data/xml/cwec_latest.xml.zip",
              "uv run --extra eval python scripts/label_with_bandit.py --roots <你的真实代码目录>",
              "uv run --extra eval python scripts/build_cwe_corpus.py",
              "HF_HUB_OFFLINE=1 uv run --extra embed --extra eval python scripts/bench_retrieval_scale.py",
              "```", "", "读法：`recall@k` 是**关掉门限**的纯排序质量；`门限0.45保留` 是生产口径下"
              "正确条款仍出现在返回结果里的比例；`干净块误放行` 是没有 finding 的代码块被塞进"
              "条款的比例。逐查询明细见 `data/corpus/scale-results.json`。"]
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
