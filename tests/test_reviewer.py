"""M4 测试：重试链、prompt 构造、幻觉校验、合并去重。全部使用 Fake LLM 离线运行。"""
import pytest
from pydantic import BaseModel

from pr_reviewer.config import Settings
from pr_reviewer.llm.base import LLMBackend, LLMOutputError, LLMUnavailableError
from pr_reviewer.models import CodeChunk, CodeReviewIssue, RuleHit
from pr_reviewer.reviewer import (NO_RULES_DECLARATION, ChunkReviewer, RetrievalCounter,
                                  merge_issues)

CHUNK = CodeChunk(
    file_path="app/auth.py", start_line=10, end_line=20, symbol="login",
    language="py", content="def login():\n    token = 'sk-hardcoded'\n",
    changed_lines=[11],
)


class FakeLLM(LLMBackend):
    """按脚本回放的假后端：元素为 dict（成功）或 Exception（触发重试）。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[str] = []

    def chat_structured(self, system: str, user: str, schema: type[BaseModel]):
        self.calls.append(user)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return schema.model_validate(item)

    def health(self) -> bool:
        return True


class FakeRetriever:
    def __init__(self, hits=None):
        self.hits = hits or []

    def retrieve(self, query, top_k=None):
        return self.hits

    def has_rule(self, rule_id):
        return rule_id in {h.rule_id for h in self.hits}


GOOD = {"issues": [{"file_path": "app/auth.py", "line_number": 11,
                    "issue_type": "security", "risk_level": "high",
                    "issue_description": "硬编码 token", "suggestion": "改用环境变量",
                    "related_rules": ["SEC-001"], "confidence": "high"}]}


def make_reviewer(llm, retriever=None, retries=2) -> ChunkReviewer:
    settings = Settings(_env_file=None, review_max_retries=retries)
    return ChunkReviewer(settings, llm, retriever)


def test_prompt_contains_code_rules_and_line_mapping():
    llm = FakeLLM([GOOD])
    rules = [RuleHit(rule_id="SEC-001", text="禁止硬编码密钥", score=0.5)]
    make_reviewer(llm, FakeRetriever(rules)).review_chunk(CHUNK)
    prompt = llm.calls[0]
    assert "<code>" in prompt and "def login" in prompt
    assert "SEC-001" in prompt and "禁止硬编码密钥" in prompt
    assert "块内第 1 行 = 文件第 10 行" in prompt   # 行号映射说明
    assert "变更行" in prompt and "11" in prompt
    # 防注入声明在 system prompt 侧（chat_structured 第一个参数，FakeLLM 只记 user，此处不测）


def test_empty_rules_declaration():
    llm = FakeLLM([GOOD])
    make_reviewer(llm, FakeRetriever([])).review_chunk(CHUNK)
    assert NO_RULES_DECLARATION in llm.calls[0]


def test_no_retriever_degrades_to_no_rag():
    llm = FakeLLM([GOOD])
    issues, status, _ = make_reviewer(llm, retriever=None).review_chunk(CHUNK)
    assert status == "ok"
    assert NO_RULES_DECLARATION in llm.calls[0]
    assert issues[0].related_rules == []  # 无检索器时引用条款一律移除


def test_retry_with_error_feedback():
    llm = FakeLLM([LLMOutputError("bad json: expect ','"), GOOD])
    issues, status, _ = make_reviewer(llm, retries=2).review_chunk(CHUNK)
    assert status == "ok" and len(issues) == 1
    assert len(llm.calls) == 2
    assert "bad json" in llm.calls[1]           # 错误信息回传
    assert "严格修正" in llm.calls[1]


def test_retry_exhausted_returns_error_status():
    llm = FakeLLM([LLMOutputError("x")] * 3)
    issues, status, _ = make_reviewer(llm, retries=2).review_chunk(CHUNK)
    assert status == "error" and issues == []
    assert len(llm.calls) == 3                   # 1 次初始 + 2 次重试


def test_unavailable_backend_fails_one_chunk_not_whole_pr():
    """限流/超时已被后端退避重试过：回灌 prompt 再问没有意义，
    必须立刻只放弃这一块（实测一次 429 让整轮评测崩掉就是因为没区分这两类错误）。
    """
    llm = FakeLLM([LLMUnavailableError("429 rate limit")])
    issues, status, _ = make_reviewer(llm, retries=2).review_chunk(CHUNK)
    assert status == "error" and issues == []
    assert len(llm.calls) == 1                   # 不消耗解析重试次数


def test_retrieval_counter_counts_hits_and_top1():
    c = RetrievalCounter()
    c.record([RuleHit(rule_id="SEC-001", text="t", score=0.61),
              RuleHit(rule_id="SEC-004", text="t", score=0.55)])
    c.record([])
    stats = c.stats(index_size=9)
    assert (stats.chunks_reviewed, stats.chunks_with_rules) == (2, 1)
    assert stats.rules_injected == 2 and stats.hit_rate == 0.5
    assert stats.avg_top1_score == 0.61 and stats.index_size == 9
    empty = RetrievalCounter().stats(index_size=9)
    assert empty.avg_top1_score is None and empty.hit_rate == 0.0


def test_hallucination_filtering():
    base = GOOD["issues"][0]
    reviewer = make_reviewer(
        FakeLLM([]), FakeRetriever([RuleHit(rule_id="SEC-001", text="t", score=0.5)]))
    issues = [CodeReviewIssue(**base),
              CodeReviewIssue(**{**base, "file_path": "other/evil.py"}),   # 幻觉1：文件不在
              CodeReviewIssue(**{**base, "line_number": 99}),              # 幻觉2：行号越界
              CodeReviewIssue(**{**base, "line_number": 12,
                                 "related_rules": ["SEC-999"]})]           # 伪造条款号
    valid, dropped = reviewer.validate_issues(issues, CHUNK)
    assert dropped == 2
    assert {i.line_number for i in valid} == {11, 12}
    fake_rule_issue = next(i for i in valid if i.line_number == 12)
    assert fake_rule_issue.related_rules == []   # SEC-999 被移除，issue 保留


def test_merge_issues_dedup_and_sort():
    mk = lambda ln, risk, desc, itype="security": CodeReviewIssue(
        file_path="a.py", line_number=ln, issue_type=itype, risk_level=risk,
        issue_description=desc, suggestion="s")
    merged = merge_issues([
        mk(10, "low", "短描述"),
        mk(12, "low", "重复问题的更详细描述，应当保留", "security"),  # 与上行同类同位置
        mk(30, "high", "高危"),
        mk(31, "low", "不同类型不合并", "style"),
    ])
    assert [i.line_number for i in merged] == [30, 12, 31]  # high 优先，重复去重
    assert "更详细描述" in merged[1].issue_description
