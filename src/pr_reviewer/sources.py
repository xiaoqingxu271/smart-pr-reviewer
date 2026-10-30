"""PR 数据源（PRD §4.1）：本地 GitPython / GitHub API / GitLab API 三种来源。

统一产出：DiffResult（diff 结构化） + file_contents dict（新文件完整内容，
供 AST 分块用）。API 模式认证走环境变量：GITHUB_TOKEN / GITLAB_TOKEN。
"""
from __future__ import annotations

import base64
from urllib.parse import quote

import httpx

from pr_reviewer.diff_extractor import DiffResult, file_change_from_patch, parse_unified_diff
from pr_reviewer.models import ChangeType

TIMEOUT = 30


# ---------------------------------------------------------------- 本地模式

def fetch_diff_local(repo_path: str, base_branch: str, head_branch: str) -> DiffResult:
    import git  # 延迟导入
    repo = git.Repo(repo_path)
    return parse_unified_diff(repo.git.diff(f"{base_branch}...{head_branch}", unified=3))


def load_contents_local(repo_path: str, head_branch: str, paths: list[str]) -> dict[str, str]:
    import git
    repo = git.Repo(repo_path)
    # 用 blob 对象 API 读取原始字节；repo.git.show() 会 strip 末尾换行，
    # 导致行号映射错位
    tree = repo.commit(head_branch).tree
    contents = {}
    for p in paths:
        try:
            contents[p] = tree[p].data_stream.read().decode()
        except (git.GitCommandError, KeyError):
            pass  # 文件在 head 不存在（如纯重命名边界），跳过
    return contents


# ---------------------------------------------------------------- GitHub

def _github_client() -> httpx.Client:
    import os
    headers = {"Accept": "application/vnd.github+json"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    return httpx.Client(base_url="https://api.github.com", headers=headers,
                        timeout=TIMEOUT)


def fetch_diff_github(repo_url: str, pr_number: int) -> tuple[DiffResult, str]:
    """返回 (DiffResult, head_sha)。rate limit 由 httpx 抛错上层处理。"""
    owner_repo = repo_url.rstrip("/").removeprefix("https://github.com/")
    with _github_client() as client:
        pr = client.get(f"/repos/{owner_repo}/pulls/{pr_number}").raise_for_status().json()
        diff_text = client.get(f"/repos/{owner_repo}/pulls/{pr_number}",
                               headers={"Accept": "application/vnd.github.diff"}
                               ).raise_for_status().text
    return parse_unified_diff(diff_text), pr["head"]["sha"]


def load_contents_github(repo_url: str, ref: str, paths: list[str]) -> dict[str, str]:
    owner_repo = repo_url.rstrip("/").removeprefix("https://github.com/")
    contents = {}
    with _github_client() as client:
        for p in paths:
            resp = client.get(f"/repos/{owner_repo}/contents/{p}", params={"ref": ref})
            if resp.is_success:
                contents[p] = base64.b64decode(resp.json()["content"]).decode("utf-8")
    return contents


# ---------------------------------------------------------------- GitLab

def _gitlab_client() -> tuple[httpx.Client, str]:
    import os
    base = os.environ.get("GITLAB_BASE_URL", "https://gitlab.com").rstrip("/")
    headers = {}
    if os.environ.get("GITLAB_TOKEN"):
        headers["PRIVATE-TOKEN"] = os.environ["GITLAB_TOKEN"]
    return httpx.Client(headers=headers, timeout=TIMEOUT), base


def fetch_diff_gitlab(repo_url: str, mr_iid: int) -> tuple[DiffResult, str]:
    """GitLab diffs 接口返回逐文件 JSON，重建为 unified diff 后解析。"""
    project = quote(repo_url.rstrip("/").removeprefix("https://gitlab.com/"), safe="")
    client, base = _gitlab_client()
    with client:
        mr = client.get(f"{base}/api/v4/projects/{project}/merge_requests/{mr_iid}"
                        ).raise_for_status().json()
        diffs = client.get(f"{base}/api/v4/projects/{project}/merge_requests/{mr_iid}/diffs"
                           ).raise_for_status().json()
    parts = []
    for d in diffs:
        parts.append(f"diff --git a/{d['old_path']} b/{d['new_path']}\n{d['diff']}")
    return parse_unified_diff("\n".join(parts)), mr["sha"]


def load_contents_gitlab(repo_url: str, ref: str, paths: list[str]) -> dict[str, str]:
    project = quote(repo_url.rstrip("/").removeprefix("https://gitlab.com/"), safe="")
    client, base = _gitlab_client()
    contents = {}
    with client:
        for p in paths:
            resp = client.get(
                f"{base}/api/v4/projects/{project}/repository/files/{quote(p, safe='')}/raw",
                params={"ref": ref})
            if resp.is_success:
                contents[p] = resp.text
    return contents


# ---------------------------------------------------------------- 统一入口

def fetch_diff(request, settings) -> tuple[DiffResult, str]:
    """按 ReviewRequest.mode 分发；返回 (DiffResult, head_ref)。

    head_ref 在本地模式为分支名，API 模式为 commit sha（取文件内容用）。
    """
    if request.mode == "local":
        if not (request.repo_path and request.base_branch and request.head_branch):
            raise ValueError("local 模式需要 repo_path / base_branch / head_branch")
        return fetch_diff_local(request.repo_path, request.base_branch,
                                request.head_branch), request.head_branch
    if not (request.repo_url and request.pr_number):
        raise ValueError("api 模式需要 repo_url / pr_number")
    if "github.com" in request.repo_url:
        return fetch_diff_github(request.repo_url, request.pr_number)
    return fetch_diff_gitlab(request.repo_url, request.pr_number)


def load_contents(request, head_ref: str, paths: list[str]) -> dict[str, str]:
    if request.mode == "local":
        return load_contents_local(request.repo_path, head_ref, paths)
    if "github.com" in request.repo_url:
        return load_contents_github(request.repo_url, head_ref, paths)
    return load_contents_gitlab(request.repo_url, head_ref, paths)
