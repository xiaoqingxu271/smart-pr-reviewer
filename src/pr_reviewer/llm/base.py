"""LLM 双后端抽象（PRD §6.4）：OpenAI（源码出内网）与 Ollama（本地隐私模式）。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TypeVar

from pydantic import BaseModel

from pr_reviewer.config import Settings

T = TypeVar("T", bound=BaseModel)


class LLMBackend(ABC):
    """统一约定：chat() 必须返回符合 schema 的结构化输出（PRD §5.4 第 1 条）。

    OpenAI: response_format=json_schema (structured outputs)
    Ollama: format=json
    """

    @abstractmethod
    def chat_structured(self, system: str, user: str, schema: type[T]) -> T:
        """结构化调用：解析失败抛 LLMOutputError，由 reviewer 负责重试。"""

    @abstractmethod
    def health(self) -> bool:
        """后端连通性检查（GET /health 使用）。"""


class LLMOutputError(Exception):
    """LLM 输出无法解析或不符合 Pydantic schema——可以通过把错误回灌 prompt 重试。"""


class LLMUnavailableError(RuntimeError):
    """后端不可用（限流 / 超时 / 连接失败 / 5xx）。

    与 LLMOutputError 必须区分：重试 prompt 修不好它。后端内部已按 PRD §6.3 做过
    指数退避，仍失败才抛这里；reviewer 收到它只把该块标记 review_error，
    绝不冒泡中断整份 PR（429 让整轮评测作废就是这么发生的）。
    """


def retry_after_sleep(attempt: int, base: float = 1.0, cap: float = 30.0,
                      retry_after: float | None = None) -> float:
    """指数退避时长：优先尊重服务端的 Retry-After，否则 base · 2^attempt 封顶 cap。"""
    if retry_after is not None:
        return min(retry_after, cap)
    return min(base * (2 ** attempt), cap)


def get_backend(settings: Settings) -> LLMBackend:
    if settings.llm_backend == "openai":
        from pr_reviewer.llm.openai_backend import OpenAIBackend
        return OpenAIBackend(settings)
    from pr_reviewer.llm.ollama_backend import OllamaBackend
    return OllamaBackend(settings)
