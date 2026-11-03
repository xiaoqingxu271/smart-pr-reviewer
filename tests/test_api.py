"""M5 API 测试：TestClient 真跑（fastapi 已安装），数据源 monkeypatch，FakeLLM 注入。

除了链路，专门覆盖落地四件事：
- Bearer 鉴权，且未配置 token 时 **fail-closed**（503）而不是裸奔；
- 任务态落 SQLite：换一个 app 实例（模拟另一个 worker / 重启）仍能查到 review_id；
- 缓存 key 用 diff 内容而不是分支名：force-push 之后不得返回上一版报告；
- 失败时给调用方友好文案，异常细节只进日志。
"""
import hashlib
import hmac
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
WEBHOOK_SECRET = "whsec-test-123"
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


def wait_for_state(client, task_id, want="completed", timeout=30.0) -> dict:
    """任务现在是真后台执行（有界线程池），不像 BackgroundTasks 那样在下一次请求前
    保证跑完——所以测试也按真实客户端的方式轮询，这本来就是 API 契约的一部分。
    窗口给到 30s：本机同时跑着 CPU 密集的评测（torch 吃满核）时，15s 会假红，
    而这不是产品缺陷。真正要钉的是状态机语义，不是"这台机器多快"。
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

    def make_app(script=None, token=TOKEN, llm=None, publisher=None, **settings_kw):
        settings = Settings(_env_file=None, chroma_dir=tmp_path / ".chroma",
                            reports_dir=tmp_path / "reports",
                            db_path=tmp_path / ".data" / "reviews.sqlite3",
                            embedding_backend="hash", api_token=token, **settings_kw)
        app = create_app(settings,
                         llm=llm or ScriptLLM(script or list(SCRIPT), usage=7),
                         retriever=None, publisher=publisher)
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
    # stale=True 同义：空索引（或索引与当前 embedding 不同空间）不用等审查失败才发现
    assert data["rag"] == {"available": False, "index_size": 0, "stale": True}
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
    detail = wait_for_state(client, first.json()["review_id"])
    assert detail["status"] == "completed"
    assert detail["result"]["summary"] == "a.py 存在硬编码密钥。"


class RecordingPublisher:
    """替身发布器：验证 webhook 走完"审查 → 写回 PR"闭环，不联网。"""

    delay = 0.0

    def __init__(self):
        self.calls = []

    def publish(self, repo_url, pr_number, commit_sha, result):
        if self.delay:
            time.sleep(self.delay)      # 真实写回要跑 GitHub API，不会比这更快
        self.calls.append({"repo": repo_url, "pr": pr_number, "sha": commit_sha,
                           "issues": len(result.issues)})
        return {"inline": len(result.issues), "deferred": [], "summary_posted": True}


def signed(body: bytes, secret: str = WEBHOOK_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def webhook_body(action="opened") -> bytes:
    return json.dumps({"action": action,
                       "repository": {"html_url": "https://github.com/acme/api"},
                       "pull_request": {"number": 7}}).encode()


def test_webhook_verifies_signature_then_queues_and_publishes(env):
    make_app, _ = env
    stub = RecordingPublisher()
    client = make_app(publisher=stub, webhook_secret=WEBHOOK_SECRET)
    body = webhook_body()
    headers = {"x-github-event": "pull_request", "x-hub-signature-256": signed(body)}

    assert client.post("/webhook/github", content=body).status_code == 403      # 缺签名
    assert client.post("/webhook/github", content=body, headers={**headers,
                     "x-hub-signature-256": "sha256=" + "0" * 64}).status_code == 403
    # 签名有效但事件不相关 → 明确忽略，且不烧 LLM 配额
    closed = webhook_body("closed")
    assert client.post("/webhook/github", content=closed,
                       headers={**headers,
                                "x-hub-signature-256": signed(closed)}).json() == {
                           "ignored": True}

    # Content type 配成 form-urlencoded：签名是对的，所以不能报 403 把人引去查密钥
    form = b"action=opened"
    resp = client.post("/webhook/github", content=form,
                       headers={**headers, "x-hub-signature-256": signed(form)})
    assert resp.status_code == 400 and "Content type" in resp.json()["detail"]

    ok = client.post("/webhook/github", content=body, headers=headers)
    assert ok.status_code == 200 and ok.json()["status"] == "queued"
    detail = wait_for_state(client, ok.json()["review_id"])
    assert detail["status"] == "completed"
    assert stub.calls == [{"repo": "https://github.com/acme/api", "pr": 7,
                           "sha": "feat", "issues": 1}]
    # 写回结果要能查：调用方得知道"报告出来了"之外，评论到底发没发出去
    assert detail["publish"] == {"status": "posted", "inline": 1, "deferred": 0,
                                 "summary_posted": True}


class SlowPublisher(RecordingPublisher):
    delay = 0.4


def test_completed_means_comments_are_already_posted(env):
    """completed 之后不该再有"稍后才发生"的副作用。

    顺序写反（先 completed 再写回）时，轮询到 completed 的调用方去 PR 上看不到评论——
    这是会撒谎的状态契约。CI 上就因为这条随机红过一次：写回只有几微秒，本地永远抢在
    轮询前面，慢一点的 runner 就复现。把写回拖慢到肉眼可见，测试才能钉住这个不变量。
    """
    make_app, _ = env
    stub = SlowPublisher()
    client = make_app(publisher=stub, webhook_secret=WEBHOOK_SECRET)
    body = webhook_body()
    headers = {"x-github-event": "pull_request", "x-hub-signature-256": signed(body)}

    ok = client.post("/webhook/github", content=body, headers=headers)
    detail = wait_for_state(client, ok.json()["review_id"])
    assert detail["status"] == "completed"
    assert len(stub.calls) == 1, "completed 时评论还没写回，状态在骗轮询方"
    assert detail["publish"]["status"] == "posted"


class BoomPublisher(RecordingPublisher):
    def publish(self, repo_url, pr_number, commit_sha, result):
        raise RuntimeError("github api 503")


def test_publish_failure_keeps_report_and_says_so(env):
    """写回挂了不该把审查判失败：报告照旧可取，失败原因进 publish 字段。"""
    make_app, _ = env
    client = make_app(publisher=BoomPublisher(), webhook_secret=WEBHOOK_SECRET)
    body = webhook_body()
    headers = {"x-github-event": "pull_request", "x-hub-signature-256": signed(body)}

    ok = client.post("/webhook/github", content=body, headers=headers)
    detail = wait_for_state(client, ok.json()["review_id"])
    assert detail["status"] == "completed"
    assert detail["publish"]["status"] == "failed"
    assert "github api 503" in detail["publish"]["reason"]
    assert client.get(f"/review/{ok.json()['review_id']}/report",
                      headers=AUTH).status_code == 200


def test_webhook_disabled_without_secret_and_without_publisher(env):
    make_app, _ = env
    body = webhook_body()
    headers = {"x-github-event": "pull_request", "x-hub-signature-256": signed(body)}
    # 没配 secret：端点停用（fail-closed），而不是接受任意伪造事件
    assert make_app(publisher=RecordingPublisher()).post(
        "/webhook/github", content=body, headers=headers).status_code == 503
    # 配了 secret 但没配写回令牌：忽略事件，不白烧 token
    client = make_app(webhook_secret=WEBHOOK_SECRET)
    assert client.post("/webhook/github", content=body, headers=headers).json() == {
        "ignored": True, "reason": "未配置 GITHUB_TOKEN，无法写回 PR"}
    assert client.get("/health").json()["webhook_configured"] is True
    assert client.get("/health").json()["publish_enabled"] is False


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


def test_retriever_is_built_once_per_app(env, monkeypatch):
    """检索器按应用建一次，不按任务建。

    local embedding 后端每次构造都要加载几百 MB 模型，按任务新建会把一份 PR 的
    审查时间拖长几十秒；"索引过期就自愈重建"也只该在一个进程生命周期里发生一次。
    """
    import pr_reviewer.rag as rag_mod

    made: list[int] = []
    base = rag_mod.StandardsRetriever

    class Counting(base):
        def __init__(self, *args, **kwargs):
            made.append(1)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(rag_mod, "StandardsRetriever", Counting)
    make_app, _ = env
    client = make_app()
    client.get("/health")
    client.post("/review", json=REQUEST, headers=AUTH)
    assert len(made) == 1, f"检索器被重复构造 {len(made)} 次"


class NothingLandedPublisher(RecordingPublisher):
    """GitHub 两条写请求都 403（令牌缺 Issues / Pull requests 写权限）时的样子。"""

    def publish(self, repo_url, pr_number, commit_sha, result):
        self.calls.append({"repo": repo_url, "pr": pr_number, "sha": commit_sha,
                           "issues": len(result.issues)})
        return {"inline": 0, "deferred": list(result.issues), "summary_posted": False,
                "errors": ["HTTP 403 Resource not accessible by personal access token"]}


def test_publish_landing_nothing_is_reported_failed(env):
    """一条评论都没落地时不能报 posted——调用方会去 PR 上找不存在的东西。"""
    make_app, _ = env
    client = make_app(publisher=NothingLandedPublisher(), webhook_secret=WEBHOOK_SECRET)
    body = webhook_body()
    headers = {"x-github-event": "pull_request", "x-hub-signature-256": signed(body)}
    ok = client.post("/webhook/github", content=body, headers=headers)
    detail = wait_for_state(client, ok.json()["review_id"])
    assert detail["status"] == "completed"                 # 审查本身是成功的
    assert detail["publish"]["status"] == "failed"
    assert "Resource not accessible" in detail["publish"]["errors"][0]
    assert detail["publish"]["deferred"] == 1
