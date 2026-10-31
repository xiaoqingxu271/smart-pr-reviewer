"""M5 流水线端到端测试（离线，FakeLLM）。

added 文件全行视为变更（含 import 的 <module> 块也送审），脚本条数须与
实际送审块数 + 1 次汇总调用对齐。
"""
from pr_reviewer import pipeline as pipeline_mod
from pr_reviewer.config import Settings
from pr_reviewer.diff_extractor import DiffResult
from pr_reviewer.llm.base import LLMOutputError
from pr_reviewer.models import FileChange, RuleHit
from pr_reviewer.pipeline import run_review
from pr_reviewer.report import render_markdown

ISSUE = {"file_path": "app/auth.py", "line_number": 3, "issue_type": "security",
         "risk_level": "high", "issue_description": "硬编码 API Key",
         "suggestion": "用环境变量"}


class ScriptLLM:
    def __init__(self, script, usage=42):
        self.script = list(script)
        self.token_usage = usage

    def chat_structured(self, system, user, schema):
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return schema.model_validate(item)

    def health(self):
        return True


def make_diff(files: list[FileChange]) -> DiffResult:
    return DiffResult(files=files)


def run(script, files: list[FileChange], contents: dict[str, str], **kw):
    settings = Settings(_env_file=None, **kw)
    return run_review(settings, make_diff(files), contents, ScriptLLM(script), None)


def test_end_to_end():
    auth = 'import os\nAPI_KEY = "sk-live-abcdef123456"\n\n\ndef get_key():\n    return API_KEY\n'
    n_lines = len(auth.splitlines())
    result = run(
        [
            {"issues": []},                                  # <module> 块（import 行）
            {"issues": [ISSUE]},                             # get_key 块
            {"issues": []},                                  # util.py 的 add 块
            {"summary": "auth.py 存在硬编码密钥（高危）；util.py 修改无风险。"},
        ],
        files=[
            FileChange(file_path="app/auth.py", change_type="added",
                       changed_lines=list(range(1, n_lines + 1))),
            FileChange(file_path="app/util.py", change_type="modified",
                       changed_lines=[2]),
            FileChange(file_path="package-lock.json", change_type="added",
                       changed_lines=[1], skip_reason="lockfile"),
        ],
        contents={
            "app/auth.py": auth,
            "app/util.py": "def add(a, b):\n    return a + b\n",
            "package-lock.json": "{}",
        },
    )
    assert result.summary.startswith("auth.py")
    assert len(result.issues) == 1 and result.issues[0].file_path == "app/auth.py"
    assert set(result.reviewed_files) == {"app/auth.py", "app/util.py"}
    assert result.skipped_files == ["package-lock.json"]
    assert result.token_usage == 42
    assert result.rag_available is False          # 无检索器 → 降级声明
    assert result.review_errors == 0


def test_deleted_file_skipped():
    result = run(
        [{"summary": "ok"}],
        files=[FileChange(file_path="old.py", change_type="deleted")],
        contents={},
    )
    assert result.skipped_files == ["old.py"]
    assert result.reviewed_files == []


def test_degraded_file_flagged():
    result = run(
        [{"issues": []}, {"summary": "ok"}],   # 1 个兜底行块 + 汇总
        files=[FileChange(file_path="bad.py", change_type="added",
                          changed_lines=[1, 2])],
        contents={"bad.py": "def broken(:\n  pass\n"},
    )
    assert result.degraded_files == ["bad.py"]


def test_chunk_error_counted_not_fatal():
    result = run(
        [LLMOutputError("bad")] * 6 + [{"summary": "ok"}],  # 2 块 × 3 次 + 汇总
        files=[FileChange(file_path="a.py", change_type="added", changed_lines=[1, 2, 3, 4, 5, 6])],
        contents={"a.py": "x = 1\ny = 2\n\n\ndef f():\n    return 3\n"},
    )
    assert result.review_errors == 2
    assert result.issues == []


def test_hallucination_counted():
    # LLM 返回一个行号越界的幻觉 issue：被过滤并计数
    result = run(
        [
            {"issues": [{**ISSUE, "file_path": "app/auth.py", "line_number": 999}]},
            {"summary": "ok"},
        ],
        files=[FileChange(file_path="app/auth.py", change_type="added",
                          changed_lines=[1, 2, 3])],
        contents={"app/auth.py": "import os\nTOKEN = 'abc123'\nconnect(TOKEN)\n"},
    )
    assert result.hallucination_filtered == 1
    assert result.issues == []


def test_fallback_summary_when_llm_summary_fails():
    result = run(
        [
            {"issues": []},                    # <module> 块（import/API_KEY 行）
            {"issues": [ISSUE]},               # get_key 块
            {"issues": []},                    # util.py 的 add 块
            LLMOutputError("summary bad"),
        ],
        files=[
            FileChange(file_path="app/auth.py", change_type="added", changed_lines=[1, 2, 3, 4, 5]),
            FileChange(file_path="app/util.py", change_type="modified", changed_lines=[2]),
        ],
        contents={
            "app/auth.py": 'import os\nAPI_KEY = "sk-live-abcdef123456"\n\n\ndef get_key():\n    return API_KEY\n',
            "app/util.py": "def add(a, b):\n    return a + b\n",
        },
    )
    assert "auth.py" in result.summary and "1 个问题" in result.summary


AUTH = 'import os\nAPI_KEY = "sk-live-abcdef123456"\n\n\ndef get_key():\n    return API_KEY\n'
AUTH_FILES = [FileChange(file_path="app/auth.py", change_type="added",
                         changed_lines=list(range(1, 7)))]


class StubRetriever:
    """最小检索器替身：命中固定条款，供仪表与报告断言。"""

    def __init__(self, hits):
        self.hits = hits

    def retrieve(self, query, top_k=None):
        return self.hits

    def has_rule(self, rule_id):
        return rule_id in {h.rule_id for h in self.hits}

    def rule_text(self, rule_id):
        return next((h.text for h in self.hits if h.rule_id == rule_id), None)

    def size(self):
        return 9


def test_retrieval_stats_and_report_line():
    """检索仪表必须进结果与报告：否则"RAG 空转"和"代码很干净"在交付物上无法区分。"""
    hits = [RuleHit(rule_id="SEC-001", text="禁止硬编码密钥", score=0.61)]
    result = run_review(Settings(_env_file=None), make_diff(AUTH_FILES),
                        {"app/auth.py": AUTH},
                        ScriptLLM([{"issues": []},
                                   {"issues": [{**ISSUE, "related_rules": ["SEC-001"]}]},
                                   {"summary": "ok"}]),
                        StubRetriever(hits))
    stats = result.retrieval
    assert stats is not None and stats.index_size == 9
    assert (stats.chunks_reviewed, stats.chunks_with_rules) == (2, 2)
    assert stats.rules_injected == 2 and stats.avg_top1_score == 0.61
    assert result.issues[0].related_rules == ["SEC-001"]

    md = render_markdown(result)
    assert "规范检索：库 9 条款" in md and "命中 2 块（100%）" in md
    assert "命中规范**：SEC-001" in md
    # 可追溯性：报告要附被引用条款的原文摘录（PRD §5.3），光给条款号不算可核对
    assert result.rule_excerpts == {"SEC-001": "禁止硬编码密钥"}
    assert "`SEC-001`：禁止硬编码密钥" in md


def test_report_declares_empty_retrieval_as_friendly_state():
    """检索全空要呈现为友好空态（依据通用常识 + 指向核对脚本），不是错误。"""
    result = run_review(Settings(_env_file=None), make_diff(AUTH_FILES),
                        {"app/auth.py": AUTH},
                        ScriptLLM([{"issues": []}, {"issues": []}, {"summary": "ok"}]),
                        StubRetriever([]))
    assert result.retrieval.avg_top1_score is None
    assert result.retrieval.chunks_with_rules == 0
    md = render_markdown(result)
    assert "未匹配到内部规范条款" in md and "check_retrieval" in md


class FakeTime:
    """可控时钟：让"超时"这条路径可确定复现，而不是靠 sleep 碰运气。"""

    def __init__(self):
        self.now = 0.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_deadline_returns_partial_result_and_marks_it(monkeypatch):
    """超预算必须只返回已审部分并显式标记：静默不完整比不完整更危险。"""
    clock = FakeTime()
    monkeypatch.setattr(pipeline_mod, "time", clock)

    class SlowLLM:
        token_usage = 0

        def __init__(self):
            self.chunks_reviewed = 0

        def chat_structured(self, system, user, schema):
            if schema.__name__ == "SummaryOutput":
                return schema.model_validate({"summary": "部分结论"})
            self.chunks_reviewed += 1
            clock.now += 500                     # 每审一块消耗 500 秒
            return schema.model_validate({"issues": []})

    three_funcs = ("def a():\n    return 1\n\n\ndef b():\n    return 2\n\n\n"
                   "def c():\n    return 3\n")
    llm = SlowLLM()
    result = run_review(Settings(_env_file=None, review_deadline_seconds=600),
                        make_diff([FileChange(file_path="x.py", change_type="added",
                                              changed_lines=list(range(1, 10)))]),
                        {"x.py": three_funcs}, llm, None)

    assert llm.chunks_reviewed == 2               # 第三块被预算挡下
    assert result.deadline_exceeded is True
    assert result.summary == "部分结论"
    assert "⏳" in render_markdown(result)         # 报告里必须看得见"只审了部分"
