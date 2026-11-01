"""PR 数据源（PRD §4.1）：本地 GitPython / GitHub API / GitLab API 三种来源。

统一产出：DiffResult（diff 结构化） + file_contents dict（新文件完整内容，
供 AST 分块用）。

凭据来源以 **Settings（也就是 .env）为准，环境变量只兜底**。GitLab 侧仍只有环境变量
（`GITLAB_TOKEN` / `GITLAB_BASE_URL`），因为 Settings 里没有对应字段。
"""
from __future__ import annotations

import base64
import contextlib
import os
import re
import warnings
from pathlib import Path
from urllib.parse import quote

import httpx

from pr_reviewer.diff_extractor import DiffResult, parse_unified_diff

TIMEOUT = 30


class RepoNotAllowed(ValueError):
    """local 模式请求了白名单之外的仓库路径（PRD §6.4）。"""


def assert_local_repo_allowed(repo_path: str, settings) -> None:
    """服务端只允许读取管理员指定目录下的仓库。

    不加这层限制时，`mode=local` + 任意 `repo_path` 等于把"读取服务器上任何 git
    仓库并把内容送去 LLM"的能力开放给了每个调用方。报错文案只说怎么才能被允许，
    不回显路径、不暴露异常细节。
    """
    roots = [Path(p).resolve() for p in
             (getattr(settings, "allowed_repo_roots", "") or "").split(",") if p.strip()]
    if not roots:
        raise RepoNotAllowed("本地仓库审查未开放：需服务管理员配置 ALLOWED_REPO_ROOTS")
    target = Path(repo_path).resolve()
    if not any(target == root or root in target.parents for root in roots):
        raise RepoNotAllowed("该仓库路径不在服务允许的目录范围内")


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
        with contextlib.suppress(git.GitCommandError, KeyError):
            # 该 ref 下不存在这个路径（纯删除、重命名边界等）→ 跳过
            contents[p] = decode_source(tree[p].data_stream.read())
    return contents


# ---------------------------------------------------------------- GitHub

def _github_token(settings=None) -> str:
    return ((getattr(settings, "github_token", "") or "")
            or os.environ.get("GITHUB_TOKEN", ""))


def _github_base(settings=None) -> str:
    return ((getattr(settings, "github_api_base", "") or "")
            or os.environ.get("GITHUB_API_BASE", "https://api.github.com"))


def _owner_repo(repo_url: str) -> str:
    """`https://<host>/owner/repo` → `owner/repo`。

    按 URL 结构剥而不是写死 github.com：自建 / 企业版换了 host 时，写死会让所有
    接口路径变成 `repos/https://git.corp/o/r/...`，报 404 而看不出原因。
    """
    match = re.match(r"^[a-zA-Z]+://[^/]+/(.+)$", repo_url.strip())
    return (match.group(1) if match else repo_url).strip("/").removesuffix(".git")


def _github_client(settings=None) -> httpx.Client:
    """令牌与 API host 都从 Settings 取，环境变量兜底。

    为什么不能只读 os.environ：pydantic-settings 把 .env 灌进 Settings 对象，**不会**
    写回环境变量。以前取 diff / 读文件走 env、写评论走 settings，于是"在 .env 里配好
    GITHUB_TOKEN"只有一半生效——写回能用，读取仍是未认证。未认证限额 60 次/小时，
    一个 PR 就能撞墙，而症状长得像"webhook 没配好"，很难查。
    """
    headers = {"Accept": "application/vnd.github+json"}
    token = _github_token(settings)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.Client(base_url=_github_base(settings), headers=headers, timeout=TIMEOUT)


def fetch_diff_github(repo_url: str, pr_number: int, settings=None) -> tuple[DiffResult, str]:
    """返回 (DiffResult, head_sha)。rate limit 由 httpx 抛错上层处理。"""
    owner_repo = _owner_repo(repo_url)
    with _github_client(settings) as client:
        pr = client.get(f"/repos/{owner_repo}/pulls/{pr_number}").raise_for_status().json()
        diff_text = client.get(f"/repos/{owner_repo}/pulls/{pr_number}",
                               headers={"Accept": "application/vnd.github.diff"}
                               ).raise_for_status().text
    return parse_unified_diff(diff_text), pr["head"]["sha"]


def load_contents_github(repo_url: str, ref: str, paths: list[str],
                         settings=None) -> dict[str, str]:
    owner_repo = _owner_repo(repo_url)
    contents = {}
    with _github_client(settings) as client:
        for p in paths:
            resp = client.get(f"/repos/{owner_repo}/contents/{p}", params={"ref": ref})
            if resp.is_success:
                contents[p] = decode_source(base64.b64decode(resp.json()["content"]))
    return contents


# ---------------------------------------------------------------- GitLab

def _gitlab_client() -> tuple[httpx.Client, str]:
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
                contents[p] = decode_source(resp.content)
    return contents


def decode_source(raw: bytes) -> str:
    """源码字节 → 文本。

    直接 `bytes.decode()`（UTF-8 严格模式）会踩两个坑：中文团队的源码文件常是
    GBK/GB18030，抛 UnicodeDecodeError；而调用方一旦 catch 掉这个异常，该文件就
    **静默离开审查范围**——审查工具漏审比报错更危险。所以依次尝试 UTF-8 →
    GB18030，最后才用 replace 兜住（保证一定有内容可审，且不丢文件）。
    """
    for encoding in ("utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------- 浅取克隆

def _credentialed_url(repo_url: str, settings=None) -> str:
    """把 token 拼进克隆地址。返回值含凭据，**绝不参与日志与异常文本**。"""
    if "github.com" in repo_url:
        token = _github_token(settings)
    else:
        token = os.environ.get("GITLAB_TOKEN", "")
    if not token:
        return repo_url
    scheme, _, rest = repo_url.partition("://")
    return f"{scheme}://oauth2:{token}@{rest}" if rest else repo_url


def _refspecs(repo_url: str, pr_number: int) -> list[str]:
    """平台约定的 PR/MR 头引用；GitHub 用 refs/pull，GitLab 用 refs/merge-requests。"""
    if "github.com" in repo_url:
        return [f"refs/pull/{pr_number}/head"]
    return [f"refs/merge-requests/{pr_number}/head"]


def read_tree_files(repo, paths: list[str]) -> dict[str, str]:
    """从 git 对象库读 blob，而不是读 checkout 出来的工作区文本。

    两个理由：① 工作区会被 `core.autocrlf` 等过滤器改写行尾，Windows 默认就踩，
    于是送去审查的内容和 diff 的行号口径不一致；② diff 里的路径是外部可控输入，
    限定在仓库树内查找，绝对路径与 `..` 直接丢弃。
    """
    tree = repo.commit("FETCH_HEAD").tree
    contents: dict[str, str] = {}
    for p in paths:
        path = Path(p)
        if path.is_absolute() or ".." in path.parts:          # 越界路径：丢弃
            continue
        try:
            blob = tree[path.as_posix()]
        except KeyError:
            continue                                          # 该 ref 下不存在（纯删除等）
        if blob is None or blob.type != "blob":
            continue
        contents[p] = decode_source(blob.data_stream.read())
    return contents


def load_contents_clone(request, head_ref: str, paths: list[str],
                        settings=None) -> dict[str, str]:
    """一次 fetch 取到 PR 头，再从 git 对象库读全部文件内容（不 checkout 工作区）。

    为什么要换掉逐文件 contents 接口：一个 30 文件的 PR 要发 30 次请求，未认证时
    GitHub 限额 60 次/小时——一个 PR 就能把额度吃光，第二个 PR 直接失败。
    """
    import tempfile

    import git

    url = _credentialed_url(request.repo_url, settings)
    refs = _refspecs(request.repo_url, request.pr_number) + ([head_ref] if head_ref else [])
    with tempfile.TemporaryDirectory(prefix="pr-reviewer-") as tmp:
        repo = git.Repo.init(Path(tmp) / "repo")
        contents: dict[str, str] | None = None
        last_error: Exception | None = None
        try:
            repo.create_remote("origin", url)
            for ref in refs:
                # 先试 shallow（省带宽），不支持时退回普通 fetch——本地仓库/老服务端的
                # file:// 传输经常不接受 --depth，不能因此让整个 PR 审不了
                for args in (["--depth", "1", "origin", ref], ["origin", ref]):
                    try:
                        repo.git.fetch(*args)
                    except git.GitCommandError as exc:
                        last_error = exc
                        continue
                    contents = read_tree_files(repo, paths)
                    break
                if contents is not None:
                    break
        finally:
            # Windows 上不 close 会留文件句柄，临时目录删不掉并在 __exit__ 抛
            # PermissionError；因为 return 还在 with 里，异常会把已经读好的内容一起吞掉
            repo.close()
    if contents is None:
        raise RuntimeError(f"浅取克隆失败（{type(last_error).__name__}）") from last_error
    return contents


# ---------------------------------------------------------------- 统一入口

def fetch_diff(request, settings) -> tuple[DiffResult, str]:
    """按 ReviewRequest.mode 分发；返回 (DiffResult, head_ref)。

    head_ref 在本地模式为分支名，API 模式为 commit sha（取文件内容用）。
    """
    if request.mode == "local":
        if not (request.repo_path and request.base_branch and request.head_branch):
            raise ValueError("local 模式需要 repo_path / base_branch / head_branch")
        assert_local_repo_allowed(request.repo_path, settings)
        return fetch_diff_local(request.repo_path, request.base_branch,
                                request.head_branch), request.head_branch
    if not (request.repo_url and request.pr_number):
        raise ValueError("api 模式需要 repo_url / pr_number")
    if "github.com" in request.repo_url:
        return fetch_diff_github(request.repo_url, request.pr_number, settings)
    return fetch_diff_gitlab(request.repo_url, request.pr_number)


def load_contents(request, head_ref: str, paths: list[str], settings=None) -> dict[str, str]:
    if request.mode == "local":
        # 再校验一次：这个入口直接按路径读文件内容，不能只依赖上游检查
        if settings is not None:
            assert_local_repo_allowed(request.repo_path, settings)
        return load_contents_local(request.repo_path, head_ref, paths)
    if settings is None or settings.use_clone_fetch:
        try:
            return load_contents_clone(request, head_ref, paths, settings)
        except Exception as exc:  # noqa: BLE001 — 取不到就退回逐文件接口（处处可降级）
            warnings.warn(f"浅取克隆失败，退回逐文件接口：{type(exc).__name__}",
                          RuntimeWarning, stacklevel=2)
    if "github.com" in request.repo_url:
        return load_contents_github(request.repo_url, head_ref, paths, settings)
    return load_contents_gitlab(request.repo_url, head_ref, paths)
