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
    git = pytest.importorskip("git")  # 本机未装 GitPython 时跳过
    import subprocess
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    run = lambda *a, **k: subprocess.run(["git", *a], cwd=repo_dir, check=True,
                                         capture_output=True, **k)
    run("init", "-b", "main")
    run("config", "user.email", "t@t"); run("config", "user.name", "t")
    (repo_dir / "f.py").write_text("hello = 1\n")
    run("add", "."); run("commit", "-m", "init")
    run("checkout", "-b", "feat")
    (repo_dir / "f.py").write_text("hello = 2\n")
    run("add", "."); run("commit", "-m", "change")

    diff = sources.fetch_diff_local(str(repo_dir), "main", "feat")
    assert diff.files[0].file_path == "f.py"
    contents = sources.load_contents_local(str(repo_dir), "feat", ["f.py"])
    assert contents["f.py"] == "hello = 2\n"
