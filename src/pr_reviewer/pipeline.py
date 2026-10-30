"""审查流水线编排（PRD §3）：

diff → 分块（只送变更块，附相邻块 context）→ 巨块二级切分 → token 预算分批
→ 逐块 LLM 审查 + 幻觉校验 → 合并去重 → LLM 汇总 → PRReviewResult。
"""
from __future__ import annotations

import time
from pydantic import BaseModel

from pr_reviewer.chunker import chunk_file, split_oversized_chunk
from pr_reviewer.config import Settings
from pr_reviewer.diff_extractor import DiffResult
from pr_reviewer.llm.base import LLMOutputError, LLMUnavailableError
from pr_reviewer.log import get_logger
from pr_reviewer.models import CodeChunk, PRReviewResult
from pr_reviewer.reviewer import ChunkReviewer, SYSTEM_PROMPT

SUMMARY_SYSTEM = """你是代码审查报告撰写人。根据 issues 清单撰写 PR 摘要：
提及每个存在 issue 的文件，按风险等级归纳，不超过 200 字，中文输出。"""


class SummaryOutput(BaseModel):
    summary: str


def _select_chunks(settings: Settings, diff: DiffResult,
                   file_contents: dict[str, str]) -> tuple[list[CodeChunk], list[str], list[str]]:
    """产出送审块；返回 (chunks, skipped_files, degraded_files)。"""
    chunks: list[CodeChunk] = []
    skipped, degraded = [], []
    for fc in diff.files:
        if fc.change_type == "deleted":
            skipped.append(fc.file_path)
            continue
        if fc.skip_reason:
            skipped.append(fc.file_path)
            continue
        content = file_contents.get(fc.file_path)
        if content is None:
            skipped.append(fc.file_path)
            continue
        file_chunks = chunk_file(fc.file_path, content, fc.changed_lines)
        prev = None
        for c in file_chunks:
            if not c.changed_lines:      # 未变更块不送审（PRD §3 关键约束）
                prev = c
                continue
            if prev is not None:         # 相邻块签名级 context（PRD §5.2）
                c = c.model_copy(update={
                    "context": f"{prev.symbol}: {prev.content.splitlines()[0][:100]}"})
            chunks.extend(split_oversized_chunk(c, settings.chunk_token_limit))
            prev = c
        if any(c.degraded for c in file_chunks):
            degraded.append(fc.file_path)
    return chunks, skipped, degraded


def _batches(chunks: list[CodeChunk], settings: Settings) -> list[list[CodeChunk]]:
    """token 预算分批（PRD §4.5）：近似 1 token ≈ 3 字符。"""
    budget = settings.token_budget * 3
    batches, cur, size = [], [], 0
    for c in chunks:
        n = len(c.content) + len(c.context)
        if cur and size + n > budget:
            batches.append(cur)
            cur, size = [], 0
        cur.append(c)
        size += n
    if cur:
        batches.append(cur)
    return batches


def run_review(settings: Settings, diff: DiffResult, file_contents: dict[str, str],
               llm, retriever) -> PRReviewResult:
    log = get_logger()
    start = time.time()
    reviewer = ChunkReviewer(settings, llm, retriever)
    chunks, skipped, degraded = _select_chunks(settings, diff, file_contents)
    batches = _batches(chunks, settings)
    log.info("stage=chunk files=%d chunks=%d skipped=%d degraded=%d batches=%d",
             len(diff.files), len(chunks), len(skipped), len(degraded), len(batches))

    issues, hallucinated, errors = [], 0, 0
    for batch in batches:
        for chunk in batch:
            batch_issues, status, filtered = reviewer.review_chunk(chunk)
            hallucinated += filtered
            if status == "error":
                errors += 1
            issues.extend(batch_issues)

    from pr_reviewer.reviewer import merge_issues
    merged = merge_issues(issues)

    # 条款原文摘录（PRD §5.3 可追溯性）：只摘 issue 实际引用到的条款
    cited_ids = sorted({rid for issue in merged for rid in issue.related_rules})
    excerpts: dict[str, str] = {}
    if retriever is not None and cited_ids and hasattr(retriever, "rule_text"):
        excerpts = {rid: t for rid in cited_ids if (t := retriever.rule_text(rid))}

    issues_text = "\n".join(
        f"- [{i.risk_level}] {i.file_path}:{i.line_number} {i.issue_description}"
        for i in merged) or "（无 issue）"
    summary = _fallback_summary(merged)
    try:
        summary = llm.chat_structured(
            SUMMARY_SYSTEM,
            f"变更文件：{[f.file_path for f in diff.files]}\n"
            f"issues 摘要：\n{issues_text}",
            SummaryOutput,
        ).summary
    except (LLMOutputError, LLMUnavailableError) as exc:
        # 汇总失败不阻断：限流/超时同样只降级为兜底摘要。
        # 只捕 LLMOutputError 时，一次 429 会把已经审完的全部 issue 一起报废。
        log.info("stage=summary_degraded reason=%s", type(exc).__name__)

    retrieval = (reviewer.counter.stats(retriever.size())
                 if retriever is not None else None)
    log.info(
        "stage=done issues=%d hallucination_filtered=%d review_errors=%d tokens=%d "
        "duration=%.2fs rag=%s rules_injected=%s hit_rate=%s avg_top1=%s",
        len(merged), hallucinated, errors, getattr(llm, "token_usage", 0),
        time.time() - start, retrieval is not None,
        retrieval.rules_injected if retrieval else "-",
        f"{retrieval.hit_rate:.2f}" if retrieval and retrieval.chunks_reviewed else "-",
        retrieval.avg_top1_score if retrieval else "-",
    )
    return PRReviewResult(
        summary=summary.strip(),
        issues=merged,
        reviewed_files=sorted({c.file_path for c in chunks}),
        skipped_files=skipped,
        degraded_files=degraded,
        token_usage=getattr(llm, "token_usage", 0),
        duration_seconds=round(time.time() - start, 2),
        model=settings.model_name,
        hallucination_filtered=hallucinated,
        review_errors=errors,
        rag_available=retriever is not None,
        retrieval=retrieval,
        rule_excerpts=excerpts,
    )


def _fallback_summary(issues) -> str:
    if not issues:
        return "未发现明显风险问题。"
    high = sum(1 for i in issues if i.risk_level == "high")
    return (f"共发现 {len(issues)} 个问题（高危 {high}），"
            f"涉及文件：{sorted({i.file_path for i in issues})}。")
