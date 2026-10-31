"""把审查结果发回 PR：行内评论 + 一条摘要评论（PRD §6.1 的价值闭环）。

为什么要发行内评论而不是只给一份报告文件：审查要进到开发者的工作流里才有价值——
问题出现在哪一行、依据哪条规范，就在哪一行下面看到。

必须处理的现实约束：GitHub 只接受挂在 diff 覆盖行上的行内评论，行号对不上会返回
422。所以发不上的条目**退进摘要评论**而不是丢弃——静默丢条目等于漏审。
"""
from __future__ import annotations

from pr_reviewer.log import get_logger
from pr_reviewer.models import PRReviewResult

RISK_LABEL = {"high": "🔴 HIGH", "medium": "🟠 MEDIUM", "low": "🟡 LOW"}


def repo_slug(repo_url: str) -> str:
    """`https://github.com/owner/repo` → `owner/repo`（兼容结尾 / 与 .git）。"""
    path = repo_url.split("github.com", 1)[-1] if "github.com" in repo_url else repo_url
    return path.strip("/").removesuffix(".git")


def format_issue(issue, excerpt: str | None = None, with_location: bool = True) -> str:
    lines = [f"**{RISK_LABEL[issue.risk_level]} · {issue.issue_description}**"
             f"（{issue.issue_type}）"]
    if with_location:
        lines.append(f"`{issue.file_path}:{issue.line_number}`")
    lines.append(f"**建议**：{issue.suggestion}")
    if issue.related_rules:
        lines.append("**命中规范**：" + ", ".join(issue.related_rules))
        if excerpt:
            lines.append(f"> {excerpt}")
    return "\n\n".join(lines)


class GitHubPublisher:
    """用 PAT / GITHUB_TOKEN 往 PR 写评论。任何失败都不影响审查结果本身。"""

    def __init__(self, token: str, api_base: str = "https://api.github.com",
                 client=None, timeout: int = 30):
        self.token = token
        self.api_base = api_base.rstrip("/")
        self._client = client
        self.timeout = timeout

    def _http(self):
        if self._client is None:
            import httpx
            self._client = httpx.Client(
                timeout=self.timeout,
                headers={"Authorization": f"Bearer {self.token}",
                         "Accept": "application/vnd.github+json",
                         "X-GitHub-Api-Version": "2022-11-28"})
        return self._client

    def _url(self, path: str) -> str:
        """拼绝对 URL：不依赖注入 client 的 base_url，测试与生产行为一致。"""
        return f"{self.api_base}{path}"

    def publish(self, repo_url: str, pr_number: int, commit_sha: str,
                result: PRReviewResult) -> dict:
        """返回 {"inline": n, "deferred": [行内失败的问题], "summary_posted": bool}。"""
        log = get_logger()
        slug = repo_slug(repo_url)
        http = self._http()
        inline, deferred = 0, []
        for issue in result.issues:
            excerpt = next((result.rule_excerpts[r] for r in issue.related_rules
                            if r in result.rule_excerpts), None)
            body = format_issue(issue, excerpt)
            payload = {"body": body, "commit_id": commit_sha,
                       "path": issue.file_path, "line": issue.line_number, "side": "RIGHT"}
            resp = http.post(self._url(f"/repos/{slug}/pulls/{pr_number}/comments"),
                             json=payload)
            if resp.is_success:
                inline += 1
            else:
                # 行号不在 diff 覆盖范围内（常见于合并后行号漂移）：退进摘要评论
                log.info("stage=inline_deferred pr=%s file=%s line=%s status=%s",
                         pr_number, issue.file_path, issue.line_number, resp.status_code)
                deferred.append(issue)

        summary_body = self._summary_body(result, inline, deferred)
        resp = http.post(self._url(f"/repos/{slug}/issues/{pr_number}/comments"),
                         json={"body": summary_body})
        return {"inline": inline, "deferred": deferred,
                "summary_posted": resp.is_success}

    @staticmethod
    def _summary_body(result: PRReviewResult, inline: int, deferred) -> str:
        high = sum(1 for i in result.issues if i.risk_level == "high")
        lines = [f"## PR 智能审查（`{result.model}`）", "", result.summary, "",
                 f"共 {len(result.issues)} 个问题（高危 {high}），"
                 f"已作为行内评论标出 {inline} 个。"]
        if not result.rag_available:
            lines += ["", "> ℹ️ 规范检索不可用，本次仅依据通用安全常识审查。"]
        elif result.retrieval and not result.retrieval.chunks_with_rules:
            lines += ["", "> ℹ️ 本次未匹配到内部规范条款。"]
        for issue in deferred:
            lines += ["", "---", "", format_issue(issue, with_location=True)]
        if result.deadline_exceeded:
            lines += ["", "---", "", "> ⏳ 时间预算耗尽，本次只覆盖了部分变更块。"]
        return "\n".join(lines)
