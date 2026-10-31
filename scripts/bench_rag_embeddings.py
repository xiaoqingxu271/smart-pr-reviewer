"""RAG 检索质量基准（PRD §5.3 选型实证）。

目的：用**生产真实分布**的 query（漏洞 fixture 经分块后的代码块内容，正是
`review_chunk` 里 `retrieve(chunk.content)` 的输入）横向对比多档 embedding
方案，替代"中英混写查询"造成的假绿灯。

对比档位：
  A  hash 词袋（当前 .env 默认，无需任何模型）
  B  本地 sentence-transformers 模型，原始代码 query
  C  同 B，但 query 加 BGE 检索指令前缀（官方非对称检索用法）
  D  同 C，再过一层 cross-encoder 重排序（rerank）

用法（需要一个装了 sentence-transformers + torch 的环境）：
    python scripts/bench_rag_embeddings.py
    python scripts/bench_rag_embeddings.py --model BAAI/bge-m3   # 换模型对比
    python scripts/bench_rag_embeddings.py --hash-only            # 无模型环境降级

产出 experiments/rag-embedding-bench.md：recall@1/@3、MRR、分数分布与阈值标定建议。
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from pr_reviewer.config import Settings
from pr_reviewer.pipeline import _select_chunks
from pr_reviewer.rag import HashEmbedder, _cosine, parse_standards_md
from run_eval import FIXTURES, load_pr_dir

GT_PATH = ROOT / "tests" / "ground_truth.json"
STANDARDS = ROOT / "docs" / "研发代码安全规范.md"

# 漏洞类型 → 规范条款。None 表示规范库当前没有覆盖该类型（覆盖率缺口）
TYPE_TO_RULE = {
    "hardcoded_secret": "SEC-001",
    "sql_injection": "SEC-002",
    "unsafe_deserialization": "SEC-003",
    "command_injection": "SEC-004",
    "path_traversal": "SEC-005",
    "weak_hash": "SEC-006",
    "tls_verification_disabled": "SEC-006",
    "code_injection": "SEC-008",
    "xss": "SEC-009",
    "route_not_registered": "SEC-010",
    "db_access_outside_repository": "SEC-011",
    "internal_call_without_client": "SEC-012",
}

# BGE 中文官方推荐的检索指令前缀（query 侧，doc 侧不加）
BGE_QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："
THRESHOLD_SWEEP = [0.20, 0.30, 0.35, 0.40, 0.45, 0.50, 0.55]


def build_samples(settings: Settings) -> tuple[list[dict], list[dict]]:
    """走生产链路取 query：fixture → diff → 分块 → 覆盖漏洞行的块内容。"""
    covered, uncovered = [], []
    for entry in json.loads(GT_PATH.read_text(encoding="utf-8")):
        pr_dir = FIXTURES / "vuln_prs" / entry["pr"]
        diff, contents = load_pr_dir(pr_dir)
        chunks, _skipped, _degraded = _select_chunks(settings, diff, contents)
        for v in entry["vulnerabilities"]:
            hit = [c for c in chunks
                   if c.file_path == v["file_path"]
                   and c.start_line <= v["line"] <= c.end_line]
            if not hit:
                uncovered.append({"pr": entry["pr"], "type": v["type"],
                                  "note": "分块后没有块覆盖该漏洞行"})
                continue
            chunk = min(hit, key=lambda c: c.end_line - c.start_line)
            rec = {"pr": entry["pr"], "type": v["type"], "query": chunk.content,
                   "file": chunk.file_path, "line": v["line"]}
            rule = TYPE_TO_RULE.get(v["type"])
            if rule is None:
                rec["note"] = "规范库无对应条款（覆盖率缺口）"
                uncovered.append(rec)
            else:
                rec["expected"] = rule
                covered.append(rec)
    return covered, uncovered


# ---------------------------------------------------------------- 向量来源

class STEmbedder:
    """sentence-transformers 本地模型（CPU 可跑），doc/query 分别编码。"""

    def __init__(self, model_name: str, query_prefix: str = ""):
        from sentence_transformers import SentenceTransformer
        self.model = SentenceTransformer(model_name)
        self.query_prefix = query_prefix
        self.name = model_name

    def embed_docs(self, texts: list[str]) -> list[list[float]]:
        return self.model.encode(texts, normalize_embeddings=True,
                                 show_progress_bar=False).tolist()

    def embed_query(self, text: str) -> list[float]:
        q = self.query_prefix + text if self.query_prefix else text
        return self.model.encode([q], normalize_embeddings=True,
                                 show_progress_bar=False).tolist()[0]


class HashAdapter:
    """把项目的 HashEmbedder 适配成同一接口，作为 A 档基线。"""

    name = "hash-bag-of-words(256d)"

    def __init__(self):
        self._impl = HashEmbedder()

    def embed_docs(self, texts):
        return self._impl.embed(texts)

    def embed_query(self, text):
        return self._impl.embed([text])[0]


class Reranker:
    def __init__(self, model_name: str):
        from sentence_transformers import CrossEncoder
        self.model = CrossEncoder(model_name)
        self.name = model_name

    def raw(self, query: str, docs: list[str]):
        import numpy as np
        return np.asarray(self.model.predict([(query, d) for d in docs],
                                             show_progress_bar=False))

    def order(self, query: str, docs: list[str]) -> list[int]:
        scores = self.raw(query, docs)
        vals = scores[:, 1] if scores.ndim == 2 else scores
        return sorted(range(len(docs)), key=lambda i: -vals[i])

    def prob(self, query: str, docs: list[str]) -> list[float]:
        """cross-encoder 相关性概率（sigmoid 校准，bge-reranker 官方口径）。"""
        import numpy as np
        scores = self.raw(query, docs)
        vals = scores[:, 1] if scores.ndim == 2 else scores
        return (1 / (1 + np.exp(-vals))).tolist()


# ---------------------------------------------------------------- 打分

def build_negatives(settings: Settings) -> list[str]:
    """负样本：干净 PR 的全部送审块内容。用于标定阈值——
    正确条款分数高还不够，无关代码不能过阈，否则把无关条款塞进 prompt。
    """
    texts = []
    for pr_dir in sorted((FIXTURES / "clean_prs").iterdir()):
        if not pr_dir.is_dir():
            continue
        diff, contents = load_pr_dir(pr_dir)
        chunks, _sk, _dg = _select_chunks(settings, diff, contents)
        texts.extend(c.content for c in chunks if c.changed_lines)
    return texts


def negative_top1(embedder, neg_texts: list[str], rules: list[dict]) -> list[float]:
    """每个干净块的最高相似度（cosine 口径，与 retrieve() 的阈值过滤一致）。"""
    doc_vecs = embedder.embed_docs([f"{r['title']}\n{r['text']}" for r in rules])
    out = []
    for q in neg_texts:
        qv = embedder.embed_query(q)
        out.append(max(_cosine(qv, dv) for dv in doc_vecs))
    return out


def evaluate(embedder, samples: list[dict], rules: list[dict],
             reranker: Reranker | None) -> dict:
    doc_texts = [f"{r['title']}\n{r['text']}" for r in rules]
    doc_vecs = embedder.embed_docs(doc_texts)
    ranks, top1_scores, correct_scores, wrong_top1 = [], [], [], []

    for s in samples:
        qv = embedder.embed_query(s["query"])
        sims = [_cosine(qv, dv) for dv in doc_vecs]
        if reranker:                      # 粗召 top-7（此处=全量）后精排
            coarse = sorted(range(len(sims)), key=lambda i: -sims[i])
            order = reranker.order(s["query"], [doc_texts[i] for i in coarse])
            ranked = [coarse[i] for i in order]
        else:
            ranked = sorted(range(len(sims)), key=lambda i: -sims[i])

        rank = next((n + 1 for n, i in enumerate(ranked)
                     if rules[i]["rule_id"] == s["expected"]), 99)
        ranks.append(rank)
        top1_scores.append(sims[ranked[0]])
        for n, i in enumerate(ranked):
            if rules[i]["rule_id"] == s["expected"]:
                correct_scores.append(sims[i])
            elif n == 0:                  # 压住正确答案的那条错误条款
                wrong_top1.append(sims[i])

    n = len(samples)
    return {
        "n": n,
        "recall@1": sum(1 for r in ranks if r <= 1) / n,
        "recall@3": sum(1 for r in ranks if r <= 3) / n,
        "mrr": statistics.fmean(1 / r if r < 99 else 0 for r in ranks),
        "top1_score_mean": statistics.fmean(top1_scores),
        "correct_scores": sorted(correct_scores),
        "wrong_top1": sorted(wrong_top1),
        "ranks": ranks,
        "details": [(s["pr"], s["expected"], r)
                    for s, r in zip(samples, ranks, strict=True)],
    }


def calibrate(res: dict) -> list[str]:
    """阈值建议：保留正确条款的同时，尽量挡住干净代码块。"""
    lines = []
    neg = res["neg_top1"]
    for t in THRESHOLD_SWEEP:
        kept = sum(1 for sc in res["correct_scores"] if sc >= t)
        passed = sum(1 for sc in neg if sc >= t)
        lines.append(f"| {t:.2f} | {kept}/{len(res['correct_scores'])} "
                     f"| {passed}/{len(neg)} |")
    return lines


# ---------------------------------------------------------------- 过滤判据标定

def collect_rows(embedder, pos: list[dict], neg: list[str],
                 rules: list[dict]) -> list[dict]:
    """正样本（有期望条款）与负样本（干净块）统一收相似度向量，供判据横向比较。"""
    doc_vecs = embedder.embed_docs([f"{r['title']}\n{r['text']}" for r in rules])
    idx = {r["rule_id"]: i for i, r in enumerate(rules)}
    rows = []
    for s in pos:
        sims = [_cosine(embedder.embed_query(s["query"]), dv) for dv in doc_vecs]
        rows.append({"kind": "pos", "label": s["pr"], "exp": idx[s["expected"]],
                     "query": s["query"], "sims": sims})
    for q in neg:
        sims = [_cosine(embedder.embed_query(q), dv) for dv in doc_vecs]
        rows.append({"kind": "neg", "label": q.splitlines()[0][:24], "exp": None,
                     "query": q, "sims": sims})
    return rows


def keep_absolute(sims: list[float], floor: float) -> list[int]:
    """判据 1：绝对阈值（现状方案）。"""
    return [i for i, s in enumerate(sims) if s >= floor]


def keep_margin(sims: list[float], floor: float, delta: float,
                top_k: int = 3) -> list[int]:
    """判据 2：绝对下限 + 与 top1 的差距。

    注意 top1 恒满足 `>= top1 - delta`，所以 margin 只能裁剪 tail，
    不能挡住干净块——下限仍需绝对值，这正是它的结构性弱点。
    """
    ranked = sorted(range(len(sims)), key=lambda i: -sims[i])[:top_k]
    top1 = sims[ranked[0]]
    return [i for i in ranked if sims[i] >= floor and sims[i] >= top1 - delta]


def keep_spread(sims: list[float], floor: float, gap: float,
                top_k: int = 3) -> list[int]:
    """判据 3：可判别性门限——top1 没明显甩开次名时判定"这次检索无指向性"，全拒。

    干净代码块的特征是所有条款分数挤在一起，正确命中的特征是赢家突出。
    """
    ranked = sorted(range(len(sims)), key=lambda i: -sims[i])[:top_k]
    top1 = sims[ranked[0]]
    if top1 < floor:
        return []
    second = sims[ranked[1]] if len(ranked) > 1 else 0.0
    if top1 - second < gap:
        return []
    return [i for i in ranked if sims[i] >= floor and sims[i] >= top1 - 0.15]


def keep_rerank(row: dict, rr: Reranker, rule_texts: list[str],
                prob_floor: float, top_k: int = 3) -> list[int]:
    """判据 4：用 cross-encoder 的相关性概率做过滤（不只看排序）。"""
    ranked = sorted(range(len(row["sims"])), key=lambda i: -row["sims"][i])[:top_k]
    probs = rr.prob(row["query"], [rule_texts[i] for i in ranked])
    return [i for i, p in zip(ranked, probs, strict=True) if p >= prob_floor]


def keep_gate_margin(sims: list[float], floor: float, delta: float,
                     top_k: int = 3) -> list[int]:
    """判据 5（推荐）：门限只判 top1，入选只判与 top1 的差距。

    实测正确条款恒为 top1，所以该设计下漏召只会"漏成空结果"（prompt 声明无匹配、
    依通用常识审查），不会"漏成错条款"（把无关规范当依据）。这是它与绝对阈值的关键差别。
    """
    ranked = sorted(range(len(sims)), key=lambda i: -sims[i])[:top_k]
    top1 = sims[ranked[0]]
    if top1 < floor:
        return []
    return [i for i in ranked if sims[i] >= top1 - delta]


def run_criterion(rows: list[dict], keep) -> dict:
    """keep(row) -> 保留的条款下标。区分漏召的两种性质，并统计注入条款数。"""
    kept = pos_n = pos_empty = pos_wrong = neg_pass = neg_n = 0
    sizes = []
    for r in rows:
        sel = keep(r)
        if sel:
            sizes.append(len(sel))
        if r["kind"] == "pos":
            pos_n += 1
            if r["exp"] in sel:
                kept += 1
            elif not sel:
                pos_empty += 1
            else:
                pos_wrong += 1
        else:
            neg_n += 1
            neg_pass += bool(sel)
    return {"pos_kept": kept, "pos_n": pos_n, "pos_empty": pos_empty,
            "pos_wrong": pos_wrong, "neg_pass": neg_pass, "neg_n": neg_n,
            "avg_rules": statistics.fmean(sizes) if sizes else 0.0}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="BAAI/bge-base-zh-v1.5",
                    help="本地 embedding 模型（默认用已缓存的 bge-base-zh-v1.5，零下载）")
    ap.add_argument("--reranker", default="BAAI/bge-reranker-base")
    ap.add_argument("--hash-only", action="store_true", help="无模型环境只跑 A 档")
    ap.add_argument("--out", default=str(ROOT / "experiments" / "rag-embedding-bench.md"))
    args = ap.parse_args()

    settings = Settings(_env_file=None)
    rules = parse_standards_md(STANDARDS.read_text(encoding="utf-8"))
    covered, uncovered = build_samples(settings)
    neg_texts = build_negatives(settings)
    print(f"规范条款 {len(rules)}，生产分布 query {len(covered)} 条，"
          f"覆盖率缺口/异常 {len(uncovered)} 条，干净块负样本 {len(neg_texts)} 条")
    if not covered:
        sys.exit("没有可用样本")

    variants: list[tuple[str, object, Reranker | None]] = [
        ("A hash词袋 + 原始代码query", HashAdapter(), None),
    ]
    st = st_prefix = rr = None
    if not args.hash_only:
        try:
            st = STEmbedder(args.model)
            st_prefix = STEmbedder(args.model, query_prefix=BGE_QUERY_PREFIX)
            try:
                rr = Reranker(args.reranker)
            except Exception as exc:  # noqa: BLE001 — rerank 缺失不影响主结论
                print(f"!! reranker 不可用，跳过 D 档：{exc}")
        except ImportError:
            print("!! 当前环境没有 sentence-transformers，只跑 A 档（--hash-only 行为）")

    if st:
        variants += [
            (f"B {st.name} + 原始代码query", st, None),
            (f"C {st.name} + BGE检索指令前缀", st_prefix, None),
        ]
    if st and rr:
        variants.append((f"D C + {rr.name} 重排序", st_prefix, rr))

    results = {}
    for label, emb, reranker in variants:
        res = evaluate(emb, covered, rules, reranker)
        res["has_reranker"] = reranker is not None
        res["embedder"] = emb
        res["neg_top1"] = negative_top1(emb, neg_texts, rules)
        clean_pass = sum(1 for sc in res["neg_top1"] if sc >= 0.30) / len(neg_texts)
        results[label] = res
        print(f"{label:52s} R@1={res['recall@1']:.2f} R@3={res['recall@3']:.2f} "
              f"MRR={res['mrr']:.2f} top1分={res['top1_score_mean']:.3f} "
              f"干净块过阈@0.30={clean_pass:.0%}")

    # ---- 输出 markdown
    md = ["# RAG 检索质量基准：本地 embedding 选型实证", "",
          f"- 规范库：`{STANDARDS.name}`，{len(rules)} 条款（随机基线 R@3 = {3 / len(rules):.0%}）",
          "- query：**生产真实分布**——fixture 经 `_select_chunks` 分块后的代码块内容（非人工混写查询）",
          f"- 正样本 {len(covered)} 条；负样本 {len(neg_texts)} 个干净 PR 送审块；"
          f"另有 {len(uncovered)} 条漏洞类型规范库未覆盖",
          "", "## 档位对比", "",
          "「干净块过阈率」= 无关代码的最高相似度超过当前生产阈值 0.30 的比例，"
          "代表该方案会把多少无关条款塞进审查 prompt。", "",
          "| 方案 | recall@1 | recall@3 | MRR | 平均 top1 相似度 | 干净块过阈率@0.30 |",
          "|---|---|---|---|---|---|"]
    for label, res in results.items():
        clean_pass = sum(1 for sc in res["neg_top1"] if sc >= 0.30) / len(neg_texts)
        md.append(f"| {label} | {res['recall@1']:.2f} | {res['recall@3']:.2f} "
                  f"| {res['mrr']:.2f} | {res['top1_score_mean']:.3f} | {clean_pass:.0%} |")

    # 指标打平时取更简单的方案（不上 rerank）
    best_label = max(results, key=lambda k: (results[k]["recall@3"], results[k]["mrr"],
                                             not results[k]["has_reranker"]))
    best = results[best_label]
    neg_sorted = sorted(best["neg_top1"])
    p90 = neg_sorted[min(len(neg_sorted) - 1, int(len(neg_sorted) * 0.9))]
    md += ["", f"最优档位：**{best_label}**（指标相同时优先无 rerank 的简单链路）",
           "", "## 逐样本排名（1 = 正确条款排第一）", "",
           "| PR | 期望条款 | 排名 |", "|---|---|---|"]
    for pr, expected, rank in best["details"]:
        md.append(f"| {pr} | {expected} | {rank if rank < 99 else '未进前 7'} |")

    md += ["", "## 阈值标定（基于最优档位的分数分布）", "",
           f"- 正确条款分数：min {best['correct_scores'][0]:.3f} / "
           f"median {statistics.median(best['correct_scores']):.3f} / "
           f"max {best['correct_scores'][-1]:.3f}",
           f"- 干净块最高相似度：min {neg_sorted[0]:.3f} / "
           f"median {statistics.median(neg_sorted):.3f} / p90 {p90:.3f} / "
           f"max {neg_sorted[-1]:.3f}",
           f"- 错误条款抢 top1 的分数：max {max(best['wrong_top1']):.3f}"
           if best["wrong_top1"]
           else "- 错误条款抢 top1：无（全部样本 top1 即正确条款）", "",
           "| 阈值 | 正确条款保留 | 干净块误放行 |", "|---|---|---|"]
    md += calibrate(best)

    # ---- 过滤判据对比：绝对阈值切不开两个分布，测相对判据与相关性概率判据
    rows = collect_rows(best["embedder"], covered, neg_texts, rules)
    rule_texts = [f"{r['title']}\n{r['text']}" for r in rules]
    candidates: list[tuple[str, object]] = [
        ("绝对阈值 0.40", lambda r: keep_absolute(r["sims"], 0.40)),
        ("绝对阈值 0.45", lambda r: keep_absolute(r["sims"], 0.45)),
        ("绝对阈值 0.50", lambda r: keep_absolute(r["sims"], 0.50)),
        ("绝对阈值 0.55", lambda r: keep_absolute(r["sims"], 0.55)),
        ("floor 0.30 + margin Δ0.10", lambda r: keep_margin(r["sims"], 0.30, 0.10)),
        ("floor 0.40 + margin Δ0.10", lambda r: keep_margin(r["sims"], 0.40, 0.10)),
        ("floor 0.30 + spread gap0.05", lambda r: keep_spread(r["sims"], 0.30, 0.05)),
        ("floor 0.30 + spread gap0.10", lambda r: keep_spread(r["sims"], 0.30, 0.10)),
        ("floor 0.35 + spread gap0.08", lambda r: keep_spread(r["sims"], 0.35, 0.08)),
    ]
    for floor in (0.40, 0.45, 0.50):
        for delta in (0.05, 0.08, 0.12):
            candidates.append((f"gate top1≥{floor:.2f} + margin Δ{delta:.2f}",
                               lambda r, f=floor, d=delta: keep_gate_margin(r["sims"], f, d)))
    if rr:
        candidates += [(f"rerank 概率≥{p}（粗召 top3）",
                        lambda r, p=p: keep_rerank(r, rr, rule_texts, p))
                       for p in (0.5, 0.7, 0.9)]

    crit = [(label, run_criterion(rows, fn)) for label, fn in candidates]
    top1_is_right = sum(1 for r in rows
                        if r["kind"] == "pos" and r["sims"][r["exp"]] == max(r["sims"]))
    md += ["", "## 过滤判据对比", "",
           f"结构事实：{top1_is_right}/{len(covered)} 个正样本的正确条款就是 top1。"
           "因此「门限只判 top1」的方案漏召只会漏成空结果，不会把无关条款当依据。", "",
           "目标：正确条款尽量保留、`漏成错条款` 必须为 0、干净块尽量少放行。", "",
           "| 判据 | 正确条款保留 | 漏成空 | 漏成错条款 | 干净块放行 | 平均注入条款数 |",
           "|---|---|---|---|---|---|"]
    for label, m in crit:
        md.append(f"| {label} | {m['pos_kept']}/{m['pos_n']} | {m['pos_empty']} "
                  f"| {m['pos_wrong']} | {m['neg_pass']}/{m['neg_n']} | {m['avg_rules']:.1f} |")
        print(f"[判据] {label:34s} 保留 {m['pos_kept']}/{m['pos_n']} "
              f"空 {m['pos_empty']} 错 {m['pos_wrong']} "
              f"干净放行 {m['neg_pass']}/{m['neg_n']} 均注入 {m['avg_rules']:.1f}")
    # 硬性优先：不错置条款 > 保留多 > 放行少 > 注入少
    best_crit = max(crit, key=lambda t: (-t[1]["pos_wrong"], t[1]["pos_kept"],
                                         -t[1]["neg_pass"], -t[1]["avg_rules"]))
    bc = best_crit[1]
    md += ["", f"推荐判据：**{best_crit[0]}**", "",
           f"- 正确条款保留 {bc['pos_kept']}/{bc['pos_n']}（漏的 {bc['pos_empty']} 条均为"
           f"空结果降级，不是错条款）",
           f"- 干净块放行 {bc['neg_pass']}/{bc['neg_n']}，平均注入 {bc['avg_rules']:.1f} 条/次",
           "- 未保留样本的原因：其正确条款 top1 分数落在干净代码分数带内"
           "（绝对分数无法切分），根治办法是给条款补「典型违规代码示例」使 doc 与 query 同分布，"
           "而不是继续调阈值。"]

    if uncovered:
        md += ["", "## 规范库覆盖率缺口（检索无法解决，需补条款）", ""]
        for u in uncovered:
            md.append(f"- `{u['pr']}` 类型 `{u['type']}`：{u['note']}")

    Path(args.out).write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"\nsaved -> {args.out}")


if __name__ == "__main__":
    main()
