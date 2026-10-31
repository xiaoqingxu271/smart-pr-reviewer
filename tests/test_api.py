"""M5 API 测试：TestClient 真跑（fastapi 已安装），数据源 monkeypatch，FakeLLM 注入。

除了链路，专门覆盖落地四件事：
- Bearer 鉴权，且未配置 token 时 **fail-closed**（503）而不是裸奔；
- 任务态落 SQLite：换一个 app 实例（模拟另一个 worker / 重启）仍能查到 review_id；
- 缓存 key 用 diff 内容而不是分支名：force-push 之后不得返回上一版报告；
- 失败时给调用方友好文案，异常细节只进日志。
"""
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from pr_reviewer.api.main import create_app
from pr_reviewer.config import Settings
from pr_reviewer.models import DiffResult, FileChange
from tests.test_pipeline import ScriptLLM

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

FILE_CHANGE = [FileChange(file_path="app/a.py", change_type="added", changed_lines=[1, 2])]
DIFF_A = DiffResult(files=FILE_CHANGE, raw_diff="diff a\n+API_KEY = 'sk-abc'\n")
DIFF_B = DiffResult(files=FILE_CHANGE,
                    raw_diff="diff a\n+API_KEY = 'sk-abc'\n+export_key()\n")
CONTENTS = {"app/a.py": "API_KEY = 'sk-abc'\nexport_key()\n"}

ISSUE1 = {"issues": [{"file_path": "app/a.py", "line_number": 1, "issue_type": "security",
                      "risk_level": "high",
                      "issue_description": "硬编码密钥 sk-abcdef123456 写死在源码里",
                      "suggestion": "改用环境变量"}]}
SUMMARY1 = {"summary": "a.py 存在硬编码密钥。"}
SCRIPT = [ISSUE1, SUMMARY1]
SCRIPT_TWICE = SCRIPT + SCRIPT

REQUEST = {"mode": "local", "repo_path": "/srv/repos/app",
           "base_branch": "main", "head_branch": "feat"}


def wait_for_state(client, task_id, want="completed", timeout=15.0) -> dict:
    """任务现在是真后台执行（有界线程池），不像 BackgroundTasks 那样在下一次请求前
    保证跑完——所以测试也按真实客户端的方式轮询，这本来就是 API 契约的一部分。
    """
    deadline = time.time() + timeout
    detail = {}
    while time.time() < deadline:
        detail = client.get(f"/review/{task_id}", headers=AUTH).json()
        if detail["status"] == want:
            return detail
        time.sleep(0.05)
    return detail


@pytest.fixture
def env(tmp_path, monkeypatch):
    """返回 (make_app, state)：make_app 可再开实例（验跨实例持久化），state 控制数据源。"""
    import pr_reviewer.api.main as main_mod

    state = {"fetches": 0, "diffs": [DIFF_A], "boom": None}

    def fake_fetch(request, settings):
        if state["boom"] is not None:
            raise state["boom"]
        diff = state["diffs"][min(state["fetches"], len(state["diffs"]) - 1)]
        state["fetches"] += 1
        return diff, "feat"                  # 分支名固定：模拟 force-push 后名字不变

    def fake_contents(request, head_ref, paths, settings=None):
        return CONTENTS

    monkeypatch.setattr(main_mod, "fetch_diff", fake_fetch)
    monkeypatch.setattr(main_mod, "load_contents", fake_contents)

    def make_app(script=None, token=TOKEN, llm=None, **settings_kw):
        settings = Settings(_env_file=None, chroma_dir=tmp_path / ".chroma",
                            reports_dir=tmp_path / "reports",
                            db_path=tmp_path / ".data" / "reviews.sqlite3",
                            embedding_backend="hash", api_token=token, **settings_kw)
        app = create_app(settings,
                         llm=llm or ScriptLLM(script or list(SCRIPT), usage=7),
                         retriever=None)
        return TestClient(app)

    return make_app, state


def test_submit_poll_report(env):
    make_app, _ = env
    client = make_app()
    resp = client.post("/review", json=REQUEST, headers=AUTH)
    assert resp.status_code == 200
    task_id = resp.json()["review_id"]
    assert resp.json()["status"] == "queued"

    detail = wait_for_state(client, task_id)
    assert detail["status"] == "completed"
    assert detail["result"]["summary"] == "a.py 存在硬编码密钥。"
    assert detail["result"]["token_usage"] == 7
    # 脱敏必须发生在结果层：JSON 响应和 Markdown 一样是对外输出
    assert "sk-abcd****" in detail["result"]["issues"][0]["issue_description"]
    assert "sk-abcdef123456" not in json.dumps(detail["result"], ensure_ascii=False)

    report = client.get(f"/review/{task_id}/report", headers=AUTH)
    assert report.status_code == 200
    assert "# PR 审查报告" in report.text
    assert "sk-abcd****" in report.text and "sk-abcdef123456" not in report.text

    assert client.get("/review/nope", headers=AUTH).status_code == 404
    assert client.get("/review/nope/report", headers=AUTH).status_code == 404


def test_auth_required_and_fail_closed(env):
    make_app, _ = env
    client = make_app()
    assert client.post("/review", json=REQUEST).status_code == 401
    assert client.post("/review", json=REQUEST,
                       headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/review/x").status_code == 401
    # 没配 token 就停用接口，而不是当成公开服务跑：报告里含源码片段
    assert make_app(token="").post("/review", json=REQUEST, headers=AUTH).status_code == 503


def test_health_is_open_and_reports_rag_index(env):
    make_app, _ = env
    data = make_app().get("/health").json()               # 探活不需要 token
    assert data["status"] == "ok" and data["llm"] is True
    assert data["auth_configured"] is True
    assert data["local_mode_allowed"] is False            # 未配白名单 → local 模式关闭
    # 检索器对象存在但索引为空时 available 必须是 False：空转要在 /health 上看得见
    assert data["rag"] == {"available": False, "index_size": 0}
    assert data["db"] is True


def test_task_state_survives_new_app_instance(env):
    """另一个 worker / 重启后的实例必须查得到 review_id（进程内存 dict 做不到）。"""
    make_app, _ = env
    task_id = make_app().post("/review", json=REQUEST, headers=AUTH).json()["review_id"]

    other = make_app()                                    # 新实例、新连接、同一 SQLite 文件
    detail = wait_for_state(other, task_id)
    assert detail["status"] == "completed"
    assert detail["result"]["summary"] == "a.py 存在硬编码密钥。"
    assert other.get(f"/review/{task_id}/report", headers=AUTH).status_code == 200


def test_cache_hit_on_identical_diff(env):
    make_app, _ = env
    client = make_app(script=list(SCRIPT))
    first = client.post("/review", json=REQUEST, headers=AUTH).json()
    # 等第一次真正写完缓存，否则测的是竞态不是命中
    assert wait_for_state(client, first["review_id"])["cached"] is False

    second = client.post("/review", json=REQUEST, headers=AUTH).json()
    assert second["status"] == "queued"                   # 命中与否在任务态里体现
    detail2 = wait_for_state(client, second["review_id"])
    assert detail2["cached"] is True
    assert detail2["result"]["summary"] == "a.py 存在硬编码密钥。"


def test_force_push_does_not_return_stale_report(env):
    """回归测试：缓存 key 曾按请求体（分支名）计算。

    同一分支 force-push / 追加 commit 后分支名不变但 diff 变了，旧实现会在 24h 内
    把上一版代码的审查报告当新版返回——审查产品里这是给出错误结论，不是省 token。
    """
    make_app, state = env
    state["diffs"] = [DIFF_A, DIFF_B]
    client = make_app(script=SCRIPT_TWICE)

    first = client.post("/review", json=REQUEST, headers=AUTH).json()
    assert wait_for_state(client, first["review_id"])["status"] == "completed"

    second = client.post("/review", json=REQUEST, headers=AUTH).json()
    detail2 = wait_for_state(client, second["review_id"])
    assert detail2["status"] == "completed", detail2.get("error")
    assert detail2["cached"] is False, "diff 内容变了却命中缓存 → 会返回上一版结论"
    assert state["fetches"] == 2


def test_task_failure_returns_friendly_message(env):
    """失败要能落状态、且不把内部异常文本（可能含凭据）回显给调用方（PRD §6.4）。"""
    make_app, state = env
    client = make_app()
    state["boom"] = RuntimeError("connect to db://user:pw@host failed token=abc123")

    task_id = client.post("/review", json=REQUEST, headers=AUTH).json()["review_id"]
    detail = wait_for_state(client, task_id, "failed")
    assert detail["status"] == "failed"
    assert "内部错误" in detail["error"]
    assert "abc123" not in detail["error"] and "pw" not in detail["error"]
    assert client.get(f"/review/{task_id}/report", headers=AUTH).status_code == 409


def test_overload_rejects_cleanly_instead_of_drowning(env):
    """待处理满员时拒绝得干脆（可重试 429），而不是把线程池占满让所有请求一起变慢。"""
    make_app, _ = env
    gate = threading.Event()

    class BlockingLLM(ScriptLLM):
        def chat_structured(self, system, user, schema):
            gate.wait(timeout=15)
            return super().chat_structured(system, user, schema)

    client = make_app(llm=BlockingLLM(list(SCRIPT), usage=7), max_pending_reviews=1)
    first = client.post("/review", json=REQUEST, headers=AUTH)
    assert first.status_code == 200

    second = client.post("/review", json=REQUEST, headers=AUTH)
    assert second.status_code == 429
    assert "稍后重试" in second.json()["detail"]
    assert client.get("/health").json()["load"]["rejected_total"] == 1

    gate.set()
    task_id = first.json()["review_id"]
    for _ in range(80):
        detail = client.get(f"/review/{task_id}", headers=AUTH).json()
        if detail["status"] == "completed":
            break
        time.sleep(0.05)
    assert detail["status"] == "completed"
    assert detail["result"]["summary"] == "a.py 存在硬编码密钥。"


def test_local_repo_path_whitelist_enforced():
    """local 模式必须限制可读目录，否则任何调用方都能让服务器读磁盘上任意仓库。"""
    from pathlib import Path

    from pr_reviewer.config import Settings
    from pr_reviewer.sources import RepoNotAllowed, assert_local_repo_allowed

    root = Path(__file__).resolve().parent
    settings = Settings(_env_file=None, allowed_repo_roots=str(root))
    assert Settings(_env_file=None).allowed_repo_roots == ""   # 默认：未开放
    assert_local_repo_allowed(str(root / "fixtures"), settings)          # 白名单内放行
    with pytest.raises(RepoNotAllowed):
        assert_local_repo_allowed("C:/Users/other/.ssh", settings)       # 白名单外拒绝
    with pytest.raises(RepoNotAllowed):
        assert_local_repo_allowed(str(root), Settings(_env_file=None))   # 未配置即禁用
