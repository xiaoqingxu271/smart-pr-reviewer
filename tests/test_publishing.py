"""结果写回 PR 的发布器测试（httpx MockTransport，不联网）。

重点验证两件容易做错的事：行内评论被 GitHub 拒绝（422）时条目必须退进摘要评论
而不是静默消失；以及摘要评论里的降级声明（无 RAG / 只审了部分）要跟着出来。
"""
import json

import httpx

from pr_reviewer.models import CodeReviewIssue, PRReviewResult, RetrievalStats
from pr_reviewer.publishing import GitHubPublisher, repo_slug

ISSUES = [
    CodeReviewIssue(file_path="app/api/user.py", line_number=7, issue_type="security",
                    risk_level="high", issue_description="未通过 gateway 注册路由",
                    suggestion="改用 gateway.route()", related_rules=["SEC-010"]),
    CodeReviewIssue(file_path="app/jobs/cleanup.py", line_number=99, issue_type="style",
                    risk_level="medium", issue_description="自建常驻线程循环",
                    suggestion="注册到 scheduler", related_rules=["SEC-013"]),
]


def make_result(**kw) -> PRReviewResult:
    base = dict(summary="发现 2 个约定违规", issues=ISSUES, reviewed_files=["app"],
                model="test-model",
                rule_excerpts={"SEC-010": "对外接口必须使用 gateway.route 注册",
                               "SEC-013": "周期任务必须注册到 scheduler"})
    base.update(kw)
    return PRReviewResult(**base)


def test_repo_slug_forms():
    assert repo_slug("https://github.com/acme/api") == "acme/api"
    assert repo_slug("https://github.com/acme/api/") == "acme/api"
    assert repo_slug("https://github.com/acme/api.git") == "acme/api"


def test_inline_comments_posted_and_rejected_one_deferred():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, json.loads(request.content)))
        if request.url.path.endswith("/comments") and "/pulls/" in request.url.path:
            line = json.loads(request.content)["line"]
            # 第 99 行不在 diff 覆盖范围内 → GitHub 返回 422
            return httpx.Response(422 if line == 99 else 201, json={"id": 1})
        return httpx.Response(201, json={"id": 2})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = GitHubPublisher("tok", client=client).publish(
        "https://github.com/acme/api", 12, "deadbeef", make_result())

    assert result["inline"] == 1
    assert [i.line_number for i in result["deferred"]] == [99]
    assert result["summary_posted"] is True

    first = calls[0][2]
    assert first["commit_id"] == "deadbeef" and first["path"] == "app/api/user.py"
    assert first["line"] == 7 and first["side"] == "RIGHT"
    assert "SEC-010" in first["body"]
    assert "对外接口必须使用 gateway.route 注册" in first["body"]   # 条款原文摘录随评论发出

    summary = calls[-1][2]["body"]
    assert calls[-1][1] == "/repos/acme/api/issues/12/comments"
    assert "自建常驻线程循环" in summary, "行内失败的条目必须退进摘要评论，不能丢"


def test_summary_flags_degraded_states():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)["body"]
        return httpx.Response(201, json={"id": 1})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = make_result(rag_available=False, deadline_exceeded=True, issues=[])
    GitHubPublisher("tok", client=client).publish(
        "https://github.com/acme/api", 13, "sha", result)

    body = seen["body"]
    assert "规范检索不可用" in body
    assert "只覆盖了部分变更块" in body


def test_retrieval_empty_state_is_reported_as_friendly_note():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)["body"]
        return httpx.Response(201, json={"id": 1})

    stats = RetrievalStats(index_size=16, chunks_reviewed=4, chunks_with_rules=0)
    GitHubPublisher("tok", client=httpx.Client(
        transport=httpx.MockTransport(handler))).publish(
        "https://github.com/acme/api", 14, "sha", make_result(retrieval=stats, issues=[]))
    assert "未匹配到内部规范条款" in seen["body"]
