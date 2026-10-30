"""Markdown 报告生成（PRD §6.1 / §7.5 交付物）。"""
from __future__ import annotations

import re

from pr_reviewer.masking import mask_secrets
from pr_reviewer.models import PRReviewResult


def render_markdown(result: PRReviewResult) -> str:
    rank = {"high": 0, "medium": 1, "low": 2}
    issues = sorted(result.issues, key=lambda i: rank[i.risk_level])
    lines = [
        "# PR 审查报告",
        "",
        f"- 模型：`{result.model}`　耗时：{result.duration_seconds:.1f}s　"
        f"token：{result.token_usage}",
        f"- 审查文件 {len(result.reviewed_files)} 个　跳过 {len(result.skipped_files)} 个　"
        f"分块降级 {len(result.degraded_files)} 个　"
        f"幻觉过滤 {result.hallucination_filtered} 条　审查失败块 {result.review_errors} 个",
        "",
    ]
    if result.retrieval:
        r = result.retrieval
        lines.append(
            f"- 规范检索：库 {r.index_size} 条款　送审 {r.chunks_reviewed} 块　"
            f"命中 {r.chunks_with_rules} 块（{r.hit_rate:.0%}）　"
            f"注入条款 {r.rules_injected} 条次"
            + (f"　top1 平均分 {r.avg_top1_score:.3f}" if r.avg_top1_score is not None else ""),
        )
        lines.append("")
        if r.chunks_reviewed and not r.chunks_with_rules:
            lines += ["> ℹ️ 本次未匹配到内部规范条款，审查依据通用安全常识。"
                      "若确信该改动涉及内部规范，请检查规范库索引与检索门限标定"
                      "（`python -m scripts.check_retrieval`）。", ""]
    if not result.rag_available:
        lines += ["> ⚠️ 规范检索不可用，本次为无 RAG 降级审查", ""]
    if result.degraded_files:
        lines += [f"> ⚠️ 以下文件分块降级（AST 解析失败/巨函数切分），结果可信度降低："
                  f"{', '.join(result.degraded_files)}", ""]
    lines += ["## 摘要", "", result.summary, "", "## 问题清单", ""]
    if not issues:
        lines += ["未发现问题。", ""]
    for i, issue in enumerate(issues, 1):
        lines.append(
            f"### {i}. [{issue.risk_level.upper()}] "
            f"{issue.file_path}:{issue.line_number}"
            f"（{issue.issue_type}，置信度 {issue.confidence}）",
            )
        lines += ["", mask_secrets(issue.issue_description), "",
                  f"**建议**：{mask_secrets(issue.suggestion)}", ""]
        if issue.related_rules:
            lines += [f"**命中规范**：{', '.join(issue.related_rules)}", ""]
            for rid in issue.related_rules:
                excerpt = result.rule_excerpts.get(rid)
                if excerpt:      # 附条款原文（PRD §5.3 可追溯性），压成一行引用
                    flat = re.sub(r"\s+", " ", excerpt).strip()
                    lines += [f"> `{rid}`：{mask_secrets(flat)}", ""]
    if result.skipped_files:
        lines += ["## 跳过文件", "",
                  *[f"- `{p}`（锁文件/二进制/生成代码/删除）" for p in result.skipped_files], ""]
    return "\n".join(lines)
