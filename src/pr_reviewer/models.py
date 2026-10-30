"""核心数据模型（PRD §5.1 / §5.4）。

Diff 流水线内部结构：FileChange / DiffResult / CodeChunk
最终交付结构：CodeReviewIssue / PRReviewResult
"""
from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, Field

ChangeType = Literal["modified", "added", "renamed", "deleted"]
RiskLevel = Literal["high", "medium", "low"]
IssueType = Literal["security", "logic", "performance", "style"]
Confidence = Literal["high", "medium", "low"]


class FileChange(BaseModel):
    """单个文件的变更信息（PRD §4）。"""

    file_path: str                      # 新文件路径
    change_type: ChangeType
    changed_lines: list[int] = Field(default_factory=list)  # 新文件绝对行号
    old_path: str | None = None         # 重命名时的原路径
    skip_reason: str | None = None      # 命中噪音过滤时记录原因


class DiffResult(BaseModel):
    """需求 1.1 的统一输出：本地 GitPython 模式与 API 模式均产出该结构。"""

    files: list[FileChange]
    raw_diff: str = ""
    total_changed_lines: int = 0

    @property
    def reviewable_files(self) -> list[FileChange]:
        return [f for f in self.files if f.change_type != "deleted" and not f.skip_reason]


class CodeChunk(BaseModel):
    """智能分块产出的语法块（PRD §5.1）。"""

    file_path: str
    start_line: int                     # 含
    end_line: int                       # 含
    symbol: str                         # 类名/函数名，顶层代码为 "<module>"
    language: str
    content: str
    context: str = ""                   # 相邻块符号签名级摘要
    changed_lines: list[int] = Field(default_factory=list)
    degraded: bool = False              # 降级分块（按行切/巨函数二次切），报告需声明可信度降级

    @property
    def chunk_id(self) -> str:
        raw = f"{self.file_path}:{self.start_line}:{self.end_line}"
        return hashlib.sha1(raw.encode()).hexdigest()[:12]


class RuleHit(BaseModel):
    """规范检索命中条款（PRD §5.3）。"""

    rule_id: str                        # 如 "SEC-012"
    text: str
    score: float


class CodeReviewIssue(BaseModel):
    """需求 1.4 最终版 issue 模型。"""

    file_path: str
    line_number: int                    # 新文件绝对行号
    issue_type: IssueType
    risk_level: RiskLevel
    issue_description: str
    suggestion: str
    related_rules: list[str] = Field(default_factory=list)
    confidence: Confidence = "medium"


class RetrievalStats(BaseModel):
    """规范检索仪表（PRD §6.1）：回答"这次 RAG 到底生效了没有"。

    没有这层可观测性，检索空转（条款一条都召不回）在交付物上和"代码很干净"
    长得一模一样——本项目最初就是这样。
    """

    index_size: int = 0                   # 规范库条款数
    chunks_reviewed: int = 0              # 调用过检索的送审块数
    chunks_with_rules: int = 0            # 至少命中一条条款的块数
    rules_injected: int = 0               # 注入条款的总次数
    avg_top1_score: float | None = None   # 命中块的 top1 平均分；None = 一次都没命中

    @property
    def hit_rate(self) -> float:
        return self.chunks_with_rules / self.chunks_reviewed if self.chunks_reviewed else 0.0


class PRReviewResult(BaseModel):
    """需求 1.4 最终版报告模型。"""

    summary: str
    issues: list[CodeReviewIssue]
    reviewed_files: list[str] = Field(default_factory=list)
    skipped_files: list[str] = Field(default_factory=list)
    degraded_files: list[str] = Field(default_factory=list)
    token_usage: int = 0
    duration_seconds: float = 0.0
    model: str = ""
    hallucination_filtered: int = 0     # 幻觉后校验丢弃的 issue 数（PRD §5.4）
    review_errors: int = 0              # 重试耗尽仍失败的块数（PRD §5.4 第 2 条）
    rag_available: bool = True          # ChromaDB 不可用时降级声明（PRD §6.3）
    retrieval: RetrievalStats | None = None   # RAG 关闭时为 None
    rule_excerpts: dict[str, str] = Field(default_factory=dict)  # 条款号 → 原文摘录（PRD §5.3）


class ReviewRequest(BaseModel):
    """POST /review 请求体（PRD §6.1）。"""

    mode: Literal["local", "api"]
    repo_path: str | None = None
    base_branch: str | None = None
    head_branch: str | None = None
    repo_url: str | None = None
    pr_number: int | None = None
