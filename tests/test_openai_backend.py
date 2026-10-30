"""OpenAI 兼容后端测试：json_schema 降级、base_url 后缀纠正。全部用桩 client 离线跑。"""
import json
from types import SimpleNamespace

from pr_reviewer.config import Settings
from pr_reviewer.llm.base import LLMOutputError
from pr_reviewer.llm.openai_backend import OpenAIBackend
from pr_reviewer.reviewer import ChunkReviewOutput


def make_backend(base_url: str) -> OpenAIBackend:
    settings = Settings(_env_file=None, llm_backend="openai",
                        openai_api_key="test", openai_base_url=base_url)
    return OpenAIBackend(settings)


def stub_response(content: dict, tokens: int = 9):
    return SimpleNamespace(
        usage=SimpleNamespace(total_tokens=tokens),
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(content)))],
    )


def test_base_url_suffix_normalized():
    backend = make_backend("https://api.example.com/v1/chat/completions/")
    assert backend._base_url() == "https://api.example.com/v1"
    assert make_backend("https://api.example.com/v1")._base_url() == "https://api.example.com/v1"
    assert make_backend("")._base_url() is None  # 官方默认


def test_json_schema_fallback_on_unsupported_gateway():
    """不支持 response_format=json_schema 的网关：首次 400 → 降级普通调用。"""
    backend = make_backend("https://api.example.com/v1")
    calls = []

    class StubClient:
        pass

    def create(**kwargs):
        calls.append(kwargs)
        if "response_format" in kwargs:
            raise RuntimeError("response_format json_schema is not supported by this model")
        return stub_response({"issues": []})

    backend._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    out = backend.chat_structured("sys", "user", ChunkReviewOutput)
    assert isinstance(out, ChunkReviewOutput)
    assert len(calls) == 2
    assert "response_format" not in calls[1]


def test_parse_failure_raises_llm_output_error():
    backend = make_backend("https://api.example.com/v1")
    backend._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(
            create=lambda **kw: stub_response({"issues": "not-a-list"}))))
    try:
        backend.chat_structured("s", "u", ChunkReviewOutput)
        raise AssertionError("should raise")
    except LLMOutputError:
        pass
