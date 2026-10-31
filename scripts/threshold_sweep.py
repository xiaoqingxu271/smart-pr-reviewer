"""检索门限扫描：回答"top1 门限该定多少"，而不是拍一个数。

绝对阈值的问题在实测里很清楚：正确条款分数带与干净代码块分数带重叠
（见 experiments/rag-embedding-bench.md），门限抬高了漏条款、降低了塞无关条款。
到底漏在哪一侧、代价多大，只能把 10 个漏洞 PR 在各档门限下真跑一遍看。

    python scripts/threshold_sweep.py                     # 默认扫 0.40 / 0.45 / 0.50
    python scripts/threshold_sweep.py --thresholds 0.45 --delay 15

输出每档的：漏洞召回率、条款可追溯率、失败块数，以及逐 PR 的命中明细。
token 用量按档取差值统计（backend 计数器是实例级累加的）。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from pr_reviewer.config import Settings
from pr_reviewer.pipeline import run_review
from pr_reviewer.rag import StandardsRetriever
from run_eval import FIXTURES, GT_PATH, gt_match, load_pr_dir


def sweep(threshold, margin: float, settings: Settings, llm,
          retriever: StandardsRetriever, only: str = "") -> dict:
    """跑一轮。`threshold` 传字符串 "off" 表示关闭检索——这样同一把工具既能标定门限，
    也能做 RAG 消融对照（私有约定用例的关键实验），不必再维护两份脚本。
    """
    rag_on = threshold != "off"
    label = "off（无 RAG）" if not rag_on else f"{threshold:.2f}"
    if rag_on:
        settings.retrieval_score_threshold = float(threshold)
        settings.retrieval_margin = margin
    active = retriever if rag_on else None
    matched = total = errors = cited = issued = 0
    strict_hits = cited_hits = 0
    tokens_before = getattr(llm, "token_usage", 0)
    wanted = [frag for frag in only.split(",") if frag.strip()]
    rows = []
    for entry in json.loads(GT_PATH.read_text(encoding="utf-8")):
        if wanted and not any(frag in entry["pr"] for frag in wanted):
            continue
        diff, contents = load_pr_dir(FIXTURES / "vuln_prs" / entry["pr"])
        result = run_review(settings, diff, contents, llm, active)
        errors += result.review_errors
        issued += len(result.issues)
        cited += sum(1 for i in result.issues if i.related_rules)
        hit = 0
        for gt in entry["vulnerabilities"]:
            total += 1
            strict, by_rule = gt_match(result.issues, gt)
            if strict or by_rule:
                matched += 1
                hit += 1
            strict_hits += strict
            cited_hits += by_rule
        # 留下 issue 摘要：否则"无 RAG 也命中"这种反常结果无法判断是模型真有常识，
        # 还是判据太松（描述引用代码行导致关键词误配）
        digest = "; ".join(f"[{i.issue_type}] {i.issue_description[:70]}"
                           for i in result.issues[:2]) or "（未报问题）"
        rows.append((entry["pr"], hit, len(entry["vulnerabilities"]),
                     sorted({r for i in result.issues for r in i.related_rules}), digest,
                     # 区分"模型漏检"与"这块根本没审成"（限流打死时摘要也是空的）
                     result.review_errors))
    return {"label": label, "threshold": threshold, "matched": matched, "total": total,
            "recall": matched / total if total else 0.0,
            "strict_hits": strict_hits, "rule_cited_hits": cited_hits,
            "cite_rate": cited / issued if issued else 0.0,
            "errors": errors, "tokens": getattr(llm, "token_usage", 0) - tokens_before,
            "rows": rows}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--thresholds", default="0.40,0.45,0.50",
                    help="逗号分隔的 top1 门限档位")
    ap.add_argument("--margin", type=float, default=None, help="默认取当前配置值")
    ap.add_argument("--delay", type=float, default=12.0, help="每次审查后静默秒数（避限流）")
    ap.add_argument("--only", default="",
                    help="只跑 PR 名含这些片段的用例（逗号分隔，低成本复查）")
    ap.add_argument("--out", default=str(ROOT / "experiments" / "threshold-sweep.md"))
    args = ap.parse_args()

    settings = Settings()
    margin = args.margin if args.margin is not None else settings.retrieval_margin
    if settings.llm_backend == "openai" and not settings.openai_api_key:
        sys.exit("需要 OPENAI_API_KEY（.env），或改用 LLM_BACKEND=ollama")

    from pr_reviewer.llm import get_backend
    llm = get_backend(settings)
    if not llm.health():
        sys.exit("LLM 后端不可用，跑出来的数字没有意义")
    retriever = StandardsRetriever(settings)
    print(f"规范索引：{retriever.build_index()} 条款，embedding={settings.embedding_model}")

    results = []
    for token in [t.strip() for t in args.thresholds.split(",") if t.strip()]:
        threshold = "off" if token == "off" else float(token)
        print(f"== 档位 {token}（margin {margin}）==")
        res = sweep(threshold, margin, settings, llm, retriever, only=args.only)
        results.append(res)
        print(f"   召回 {res['matched']}/{res['total']}，可追溯率 {res['cite_rate']:.0%}，"
              f"失败块 {res['errors']}，token {res['tokens']:,}")
        if args.delay:
            time.sleep(args.delay)

    lines = ["# 检索门限扫描（真实 LLM）", "",
             f"- 模型：`{settings.model_name}`（{settings.llm_backend}），"
             f"embedding：`{settings.embedding_model}`，规范库 {retriever.size()} 条款",
             f"- 每组跑完 {results[0]['total']} 个标注漏洞；margin 固定 {margin}", "",
             "| 档位 | 漏洞召回率 | 其中严格口径 | 其中引用口径 | 条款可追溯率 | 失败块 | token |",
             "|---|---|---|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r['label']} | {r['recall']:.0%}（{r['matched']}/{r['total']}）"
                     f"| {r['strict_hits']}/{r['total']} | {r['rule_cited_hits']}/{r['total']} "
                     f"| {r['cite_rate']:.0%} | {r['errors']} | {r['tokens']:,} |")
    lines += ["", "## 逐 PR 明细（命中的规范条款）", ""]
    for r in results:
        lines += [f"### 档位 {r['label']}", "",
                  "| PR | 漏洞命中 | 引用条款 | 失败块 | 检出摘要 |",
                  "|---|---|---|---|---|"]
        lines += [f"| {pr} | {hit}/{n} | {', '.join(rules) or '（无）'} | {errs} | "
                  f"{digest.replace('|', '/')} |"
                  for pr, hit, n, rules, digest, errs in r["rows"]]
        lines.append("")
    any_errors = any(r["errors"] for r in results)
    if any_errors:
        lines.insert(3, "> ⚠️ 有审查失败的块（多为限流/超时），该组数字被低估，需重跑。")

    # 有 off 档时顺手算显著性：n 很小时"差几个"没有 p 值可信
    offs = next((r for r in results if r["label"].startswith("off")), None)
    if offs and len(results) > 1:
        for r in results:
            if r is offs:
                continue
            try:
                from scipy.stats import fisher_exact
                table = [[r["matched"], r["total"] - r["matched"]],
                         [offs["matched"], offs["total"] - offs["matched"]]]
                _, p = fisher_exact(table, alternative="greater")
                lines += ["", f"显著性（Fisher 单侧精确检验，{r['label']} vs off）：**p = {p:.4f}**"
                          f"{'，0.05 水平下显著' if p < 0.05 else '，未达 0.05 显著性'}"]
            except ImportError:      # 没装 scipy 时跳过，不影响主报表
                pass
    Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"saved -> {args.out}")
    failed = sum(r["errors"] for r in results)
    if failed:
        # 非零退出：被限流污染的表格长得很像真结论，不能只靠人记得去看告警行
        print(f"\n!! 本轮共 {failed} 个块审查失败（限流/超时），召回与可追溯数字"
              f"不可用于决策；不要采信 {args.out}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
