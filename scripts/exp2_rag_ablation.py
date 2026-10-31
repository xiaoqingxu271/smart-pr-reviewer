"""破坏性实验 2（PRD §7.3）：无 RAG vs 有 RAG 召回率对照。

需要真实 LLM 后端（.env 配置 openai 或启动 ollama）：
    python scripts/exp2_rag_ablation.py
产出 experiments/exp2-rag-ablation.md，对比两次 run_eval 的召回率与 related_rules 命中。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from pr_reviewer.config import Settings
from run_eval import evaluate


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None,
                    help="只跑前 N 个漏洞 PR + N 个干净 PR（限流时先出试点数据）")
    ap.add_argument("--delay", type=float, default=0.0, help="每次审查后静默秒数")
    args = ap.parse_args()

    settings = Settings()
    if settings.llm_backend == "openai" and not settings.openai_api_key:
        sys.exit("需要 OPENAI_API_KEY（.env）；或 LLM_BACKEND=ollama 并启动 ollama serve")

    from pr_reviewer.llm import get_backend
    from pr_reviewer.rag import StandardsRetriever

    llm = get_backend(settings)
    if not llm.health():
        sys.exit(f"LLM 后端不可用（{settings.llm_backend} / {settings.model_name}）："
                 f"先确认连通性与配额，否则两组数据都无意义")
    retriever = StandardsRetriever(settings)
    n = retriever.build_index()
    print(f"规范索引就绪：{n} 条款")

    print("== 组 A：无 RAG ==")
    a = evaluate(llm, None, settings, limit=args.limit, delay=args.delay)
    print(f"召回率 {a['recall']:.0%}，误报 {a['fp_per_pr']:.1f}/PR")

    print("== 组 B：有 RAG ==")
    b = evaluate(llm, retriever, settings, limit=args.limit, delay=args.delay)
    print(f"召回率 {b['recall']:.0%}，误报 {b['fp_per_pr']:.1f}/PR")

    dr, dfp = b["recall"] - a["recall"], b["fp_per_pr"] - a["fp_per_pr"]
    dcite = b["cite_rate"] - a["cite_rate"]
    if dr > 0:
        verdict = (f"规范条款注入使召回率 {a['recall']:.0%} → {b['recall']:.0%}（{dr:+.0%}），"
                   f"条款引用率 {a['cite_rate']:.0%} → {b['cite_rate']:.0%}。"
                   "结论：RAG 把「公司私有规范」补进通用模型不具备的知识边界，"
                   "增益可归因于检索到的条款本身。")
    elif dcite > 0:
        verdict = (f"召回率未拉开差距（{a['recall']:.0%} → {b['recall']:.0%}）——该模型对这类常见"
                   f"漏洞本就有常识；但条款引用率 {a['cite_rate']:.0%} → {b['cite_rate']:.0%}，"
                   "这是无 RAG 时结构性做不到的：审查结论能指回具体规范条文。")
    else:
        verdict = ("本轮未观察到净收益：需回查检索命中率（scripts/check_retrieval.py）"
                   "或扩大样本，不做无依据的增益声明。")

    validity = ("" if a["errors"] == b["errors"] == 0 else
                f"\n> ⚠️ 数据有效性：本轮共 {a['errors'] + b['errors']} 个块审查失败"
                f"（多为网关限流 429 / 超时），召回与误报都被系统性低估，"
                f"结论需在配额恢复后重跑才成立。\n")
    pilot = ("" if not args.limit else
             f"\n> 本轮是 `--limit {args.limit}` 试点样本（每组各 {a['n_vuln_prs']} 个漏洞 PR + "
             f"{a['n_clean']} 个干净 PR），指标只用于判断方向，不足以对标 PRD 目标值。\n")

    md = f"""# 破坏性实验 2：规范检索（RAG）消融对照（PRD §7.3）

- 后端：`{settings.model_name}`（{settings.llm_backend}），规范库 {n} 条款
- embedding：`{settings.embedding_backend}` / `{settings.embedding_model}`；
  判据 top1≥{settings.retrieval_score_threshold} + margin {settings.retrieval_margin}
- 同一批 {a['n_vuln_prs']} 个漏洞 PR + {a['n_clean']} 个干净 PR，
  唯一变量：是否检索规范条款注入 prompt

| 指标 | 无 RAG（组 A） | 有 RAG（组 B） | 变化 |
|---|---|---|---|
| 召回率 | {a['recall']:.0%}（{a['matched']}/{a['total']}） | {b['recall']:.0%}（{b['matched']}/{b['total']}） | {dr:+.0%} |
| 平均误报 / 干净 PR | {a['fp_per_pr']:.1f} | {b['fp_per_pr']:.1f} | {dfp:+.1f} |
| 条款引用率 | {a['cite_rate']:.0%} | {b['cite_rate']:.0%} | {dcite:+.0%} |
| token 用量 | {a['tokens']:,} | {b['tokens']:,} | {b['tokens'] - a['tokens']:+,} |
| 墙钟耗时(s) | {a['duration']} | {b['duration']} | {b['duration'] - a['duration']:+.1f} |
| 审查失败块（限流/超时/解析耗尽） | {a['errors']} | {b['errors']} | — |
{validity}{pilot}
## 结论

> {verdict}

## 局限（必须一起讲）

- 样本仅 {a['n_vuln_prs']} 个漏洞 PR、每类 1 例：单例波动就能让召回率跳动 10 个百分点，
  差异不足 2 例时不应宣称显著。
- 单一模型；匹配规则是「文件相同 ∧ 行号差 ≤3 ∧ 类型关键词命中」，关键词表本身影响判定。
- 干净 PR 只有 {a['n_clean']} 个，误报率置信区间宽；fixture 是人工构造的典型模式，
  模型可能见过同类样本，真实仓库分布会更难。
"""
    out = ROOT / "experiments" / "exp2-rag-ablation.md"
    out.write_text(md, encoding="utf-8")
    print(md)


if __name__ == "__main__":
    main()
