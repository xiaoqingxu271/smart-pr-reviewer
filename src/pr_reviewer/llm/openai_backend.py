"""OpenAI 兼容后端：structured outputs（response_format=json_schema）+ token 统计。

兼容自定义端点：OPENAI_BASE_URL 填到 /v1 级别（SDK 自动拼接 /chat/completions）；
误填了完整 /chat/completions 后缀也会被自动纠正。部分 OpenAI 兼容网关
（GLM/DeepSeek 等）不支持 response_format=json_schema，此时自动降级为
「prompt 约定 JSON + Pydantic 校验」。
"""
from __future__ import annotations

import json

from pydantic import BaseModel, ValidationError

from pr_reviewer.config import Settings
from pr_reviewer.llm.base import LLMBackend, LLMOutputError, LLMUnavailableError

_SCHEMA_UNSUPPORTED_KEYWORDS = ("response_format", "json_schema", "schema",
                                "not support", "unsupported")


class OpenAIBackend(LLMBackend):
    def __init__(self, settings: Settings):
        self.settings = settings
        self.token_usage = 0
        self._client = None

    def _base_url(self) -> str | None:
        url = self.settings.openai_base_url.rstrip("/")
        # 用户误填完整端点后缀时纠正（SDK 会再拼一次 /chat/completions）
        if url.endswith("/chat/completions"):
            url = url[: -len("/chat/completions")]
        return url or None

    def _ensure_client(self):
        if self._client is None:
            from openai import OpenAI
            # 指数退避交给 SDK（尊重 Retry-After、只重试 408/429/5xx 与连接错误，
            # PRD §6.3），不在业务层重复造轮子
            self._client = OpenAI(api_key=self.settings.openai_api_key,
                                  base_url=self._base_url(),
                                  timeout=self.settings.llm_timeout_seconds,
                                  max_retries=self.settings.llm_max_retries)
        return self._client

    @staticmethod
    def _to_unavailable(exc: Exception) -> Exception:
        """把 SDK 的网络/状态错误归一化为 LLMUnavailableError。

        不归一化的后果：429 会一路冒泡穿过 reviewer 和 pipeline，把整份 PR 的审查
        判为任务失败——一次限流废掉一次评审。
        """
        from openai import APIConnectionError, APIStatusError
        if isinstance(exc, APIConnectionError):        # 含 APITimeoutError
            return LLMUnavailableError(f"OpenAI 后端连接失败：{exc}")
        if isinstance(exc, APIStatusError):            # 含 RateLimitError / 5xx
            return LLMUnavailableError(f"OpenAI 后端不可用（HTTP {exc.status_code}）")
        return exc

    def _parse(self, schema: type[BaseModel], content: str):
        try:
            return schema.model_validate(json.loads(content))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise LLMOutputError(f"OpenAI 输出不符 schema: {exc}") from exc

    def chat_structured(self, system: str, user: str, schema: type[BaseModel]):
        client = self._ensure_client()
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": user}]
        try:
            try:
                resp = client.chat.completions.create(
                    model=self.settings.model_name,
                    messages=messages,
                    response_format={
                        "type": "json_schema",
                        "json_schema": {
                            "name": schema.__name__,
                            "schema": schema.model_json_schema(),
                            "strict": True,
                        },
                    },
                )
            except Exception as exc:  # noqa: BLE001 — 网关不支持 json_schema 时降级
                if not any(k in str(exc).lower() for k in _SCHEMA_UNSUPPORTED_KEYWORDS):
                    raise
                resp = client.chat.completions.create(model=self.settings.model_name,
                                                      messages=messages)
        except Exception as exc:  # noqa: BLE001 — 归一化为后端不可用，由 reviewer 计错
            raise self._to_unavailable(exc) from exc
        if resp.usage:
            self.token_usage += resp.usage.total_tokens
        return self._parse(schema, resp.choices[0].message.content)

    def health(self) -> bool:
        try:
            self._ensure_client().models.list()
            return True
        except Exception:  # noqa: BLE001 — 探活用
            return False
