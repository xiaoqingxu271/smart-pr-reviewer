"""M5 API 测试：TestClient 真跑（fastapi 已安装），数据源 monkeypatch，FakeLLM 注入。"""
import pytest
from fastapi.testclient import TestClient

from pr_reviewer.api.main import create_app
from pr_reviewer.config import Settings
from pr_reviewer.diff_extractor import DiffResult
from pr_reviewer.models import FileChange
from tests.test_pipeline import ScriptLLM

DIFF = DiffResult(files=[FileChange(file_path="app/a.py", change_type="added",
                                    changed_lines=[1, 2])])
CONTENTS = {"app/a.py": "API_KEY = 'sk-abc'\nexport_key()\n"}
SCRIPT = [
    {"issues": [{"file_path": "app/a.py", "line_number": 1, "issue_type": "security",
                 "risk_level": "high", "issue_description": "硬编码密钥",
                 "suggestion": "环境变量"}]},
    {"summary": "a.py 存在硬编码密钥。"},
]

REQUEST = {"mode": "local", "repo_path": "/tmp/repo",
           "base_branch": "main", "head_branch": "feat"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    import pr_reviewer.api.main as main_mod

    def fake_fetch(request, settings):
        return DIFF, "feat"

    def fake_contents(request, head_ref, paths):
        return CONTENTS

    monkeypatch.setattr(main_mod, "fetch_diff", fake_fetch)
    monkeypatch.setattr(main_mod, "load_contents", fake_contents)
    # embedding_backend=hash：API 测试只验证任务链路，不依赖真实 embedding
    settings = Settings(_env_file=None, chroma_dir=tmp_path / ".chroma",
                        reports_dir=tmp_path / "reports", embedding_backend="hash")
    app = create_app(settings, llm=ScriptLLM(list(SCRIPT), usage=7), retriever=None)
    return TestClient(app)


def test_submit_poll_report(client):
    resp = client.post("/review", json=REQUEST)
    assert resp.status_code == 200
    task_id = resp.json()["review_id"]
    assert resp.json()["status"] == "queued"

    detail = client.get(f"/review/{task_id}").json()
    assert detail["status"] == "completed"
    assert detail["result"]["summary"] == "a.py 存在硬编码密钥。"
    assert detail["result"]["token_usage"] == 7

    report = client.get(f"/review/{task_id}/report")
    assert report.status_code == 200
    assert "# PR 审查报告" in report.text
    assert "sk-abc" not in report.text or "sk-a****" in report.text  # 脱敏

    assert client.get("/review/nope").status_code == 404
    assert client.get("/review/nope/report").status_code == 404


def test_cache_hit_on_same_diff(client):
    first = client.post("/review", json=REQUEST).json()
    assert first["status"] == "queued"
    detail = client.get(f"/review/{first['review_id']}").json()
    assert detail["status"] == "completed"
    second = client.post("/review", json=REQUEST).json()
    assert second["cached"] is True
    assert second["status"] == "completed"
    detail2 = client.get(f"/review/{second['review_id']}").json()
    assert detail2["result"]["summary"] == detail["result"]["summary"]


def test_health(client):
    data = client.get("/health").json()
    assert data["status"] == "ok"
    assert data["llm"] is True
    assert data["rag"] is False
