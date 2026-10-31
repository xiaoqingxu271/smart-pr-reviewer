"""数据源测试：GitHub/GitLab 用 MockTransport；本地模式缺 GitPython 时跳过。"""
import httpx
import pytest

import pr_reviewer.sources as sources
from pr_reviewer.models import ReviewRequest

GH_DIFF = """diff --git a/app/x.py b/app/x.py
new file mode 100644
--- /dev/null
+++ b/app/x.py
@@ -0,0 +1,2 @@
+API_KEY = 'k'
+print()
"""
GH_PR_JSON = {"head": {"sha": "abc123"}}


def run_git(cwd, *args):
    """在指定目录跑 git；返回 CompletedProcess（stdout 已捕获）。"""
    import subprocess

    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_fetch_diff_github(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        accept = request.headers.get("accept", "")
        if "diff" in accept:
            return httpx.Response(200, text=GH_DIFF)
        return httpx.Response(200, json=GH_PR_JSON)

    monkeypatch.setattr(sources, "_github_client",
                        lambda: httpx.Client(transport=httpx.MockTransport(handler),
                                             base_url="https://api.github.com"))
    diff, sha = sources.fetch_diff_github("https://github.com/o/r", 7)
    assert sha == "abc123"
    assert diff.files[0].file_path == "app/x.py"
    assert diff.files[0].change_type == "added"
    assert diff.files[0].changed_lines == [1, 2]


def test_fetch_diff_gitlab(monkeypatch):
    project_diffs = [{"old_path": "app/y.py", "new_path": "app/y.py",
                      "diff": "@@ -1,2 +1,3 @@\n a\n-b\n+c\n+import os\n"}]
    mr_json = {"sha": "def456"}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/diffs"):
            return httpx.Response(200, json=project_diffs)
        return httpx.Response(200, json=mr_json)

    monkeypatch.setattr(sources, "_gitlab_client",
                        lambda: (httpx.Client(transport=httpx.MockTransport(handler)),
                                 "https://gitlab.example"))
    diff, sha = sources.fetch_diff_gitlab("https://gitlab.com/g/p", 3)
    assert sha == "def456"
    fc = diff.files[0]
    assert fc.file_path == "app/y.py"
    assert fc.changed_lines == [2, 3]     # 新文件第 2、3 行


def test_fetch_dispatch_local_validation():
    with pytest.raises(ValueError):
        sources.fetch_diff(ReviewRequest(mode="local"), None)


def test_load_contents_local(tmp_path):
    pytest.importorskip("git")  # 本机未装 GitPython 时跳过
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    run_git(repo_dir, "init", "-b", "main")
    run_git(repo_dir, "config", "user.email", "t@t")
    run_git(repo_dir, "config", "user.name", "t")
    (repo_dir / "f.py").write_text("hello = 1\n", encoding="utf-8")
    run_git(repo_dir, "add", ".")
    run_git(repo_dir, "commit", "-m", "init")
    run_git(repo_dir, "checkout", "-b", "feat")
    (repo_dir / "f.py").write_text("hello = 2\n", encoding="utf-8")
    run_git(repo_dir, "add", ".")
    run_git(repo_dir, "commit", "-m", "change")

    diff = sources.fetch_diff_local(str(repo_dir), "main", "feat")
    assert diff.files[0].file_path == "f.py"
    contents = sources.load_contents_local(str(repo_dir), "feat", ["f.py"])
    assert contents["f.py"] == "hello = 2\n"


# ---------------------------------------------------------------- 浅取克隆（省限额）

def make_origin(tmp_path, pr_content: str, encoding: str = "utf-8"):
    """造一个带 refs/merge-requests/1/head 的本地"远端"仓库。"""
    origin = tmp_path / "origin"
    origin.mkdir()
    run_git(origin, "init", "-b", "main")
    run_git(origin, "config", "user.email", "t@t")
    run_git(origin, "config", "user.name", "t")
    (origin / "app").mkdir()
    (origin / "app" / "x.py").write_text("v = 1\n", encoding="utf-8")
    run_git(origin, "add", ".")
    run_git(origin, "commit", "-m", "base")
    run_git(origin, "checkout", "-b", "pr")
    (origin / "app" / "x.py").write_bytes(pr_content.encode(encoding))   # 可造 GBK 文件
    run_git(origin, "add", ".")
    run_git(origin, "commit", "-m", "pr head")
    sha = run_git(origin, "rev-parse", "HEAD").stdout.decode().strip()
    run_git(origin, "update-ref", "refs/merge-requests/1/head", sha)
    return origin


def test_load_contents_uses_single_fetch(tmp_path):
    """一次 fetch 换掉 N 次 contents 请求，且取到的是 PR 头那一版内容。

    故意用 GBK 编码的中文源码：非 UTF-8 文件曾被静默跳过，等于该文件不参与审查。
    """
    pytest.importorskip("git")
    from pr_reviewer.config import Settings

    origin = make_origin(tmp_path, "v = 2  # 中文注释\n", encoding="gbk")
    request = ReviewRequest(mode="api", repo_url=origin.as_posix(), pr_number=1)
    contents = sources.load_contents(request, "", ["app/x.py"], Settings(_env_file=None))
    assert contents == {"app/x.py": "v = 2  # 中文注释\n"}


def test_decode_source_handles_utf8_gbk_and_binary():
    assert sources.decode_source("价格 ok".encode()) == "价格 ok"
    assert sources.decode_source("价格 ok".encode("gbk")) == "价格 ok"
    assert sources.decode_source(b"\xff\xfe\x00\x01bad")          # 兜住，不抛也不丢
    assert sources.decode_source(b"") == ""


def test_read_tree_files_scopes_to_repo_and_keeps_blob_line_endings(tmp_path):
    """读 blob 而非工作区：绝对路径/`..` 一律丢弃，且不被 core.autocrlf 改写行尾。

    后者是被 Windows 默认配置测出来的——checkout 出来的文本变成 CRLF 后，送去审查的
    内容与 diff 的行号口径就不一致了。
    """
    pytest.importorskip("git")
    import git

    origin = make_origin(tmp_path, "v = 2\n")
    repo = git.Repo.init(tmp_path / "clone")
    repo.create_remote("origin", origin.as_posix())
    repo.git.fetch("origin", "refs/merge-requests/1/head")

    out = sources.read_tree_files(repo, ["app/x.py", "/etc/passwd", "../secret.txt"])
    assert set(out) == {"app/x.py"}
    assert out["app/x.py"] == "v = 2\n"


def test_clone_failure_falls_back_to_per_file_api(monkeypatch):
    """取不到就退回逐文件接口：降级而不是整份审查失败（PRD §6.3）。"""
    from pr_reviewer.config import Settings

    def boom(*_args):
        raise RuntimeError("git 不可用")

    called: dict[str, bool] = {}

    def fake_github(url, ref, paths):
        called["yes"] = True
        return {"f.py": "x"}

    monkeypatch.setattr(sources, "load_contents_clone", boom)
    monkeypatch.setattr(sources, "load_contents_github", fake_github)
    request = ReviewRequest(mode="api", repo_url="https://github.com/o/r", pr_number=1)
    with pytest.warns(RuntimeWarning):
        out = sources.load_contents(request, "sha", ["f.py"], Settings(_env_file=None))
    assert called.get("yes") and out == {"f.py": "x"}


def test_clone_can_be_disabled_by_config(monkeypatch):
    """运维要能明确关掉克隆路径（比如镜像仓库不支持任意 ref fetch）。"""
    from pr_reviewer.config import Settings

    used: dict[str, bool] = {}
    monkeypatch.setattr(sources, "load_contents_github",
                        lambda url, ref, paths: used.setdefault("api", True) or {})
    request = ReviewRequest(mode="api", repo_url="https://github.com/o/r", pr_number=1)
    sources.load_contents(request, "sha", ["f.py"],
                          Settings(_env_file=None, use_clone_fetch=False))
    assert used.get("api") is True
