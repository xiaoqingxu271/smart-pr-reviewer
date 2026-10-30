"""Ollama 本地后端（隐私模式，代码不出内网，PRD §6.4）：format=json + schema 提示。

限流/超时/连接失败按 PRD §6.3 做指数退避重试，仍失败抛 LLMUnavailableError——
reviewer 只把该块计为 review_error，不让一次抖动废掉整份 PR。
"""
from __future__ import annotations

import json
import time

import httpx
from pydantic import BaseModel, ValidationError

from pr_reviewer.config import Settings
from pr_reviewer.llm.base import (LLMBackend, LLMOutputError, LLMUnavailableError,
                                  retry_after_sleep)


class OllamaBackend(LLMBackend):
    RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}

    def __init__(self, settings: Settings):
        self.settings = settings
        self.token_usage = 0

    def _post(self, payload: dict) -> dict:
        url = f"{self.settings.ollama_base_url.rstrip('/')}/api/chat"
        timeout = self.settings.ollama_timeout_seconds   # 本地 CPU 推理需要更长超时
        last_error: Exception | None = None
        for attempt in range(self.settings.llm_max_retries + 1):
            try:
                resp = httpx.post(url, json=payload, timeout=timeout)
                if resp.status_code in self.RETRYABLE_STATUS:
                    last_error = httpx.HTTPStatusError("限流/服务端错误", request=resp.request,
                                                       response=resp)
                    time.sleep(retry_after_sleep(attempt))
                    continue
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt < self.settings.llm_max_retries:
                    time.sleep(retry_after_sleep(attempt))
        raise LLMUnavailableError(
            f"Ollama 后端不可用（已退避重试 {self.settings.llm_max_retries} 次）："
            f"请确认 `ollama serve` 已启动且模型已 pull（{type(last_error).__name__}）"
        )

    def chat_structured(self, system: str, user: str, schema: type[BaseModel]):
        # Ollama 原生 structured outputs：format 接受 JSON schema（0.5+ 版本）
        data = self._post({
            "model": self.settings.model_name,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "format": schema.model_json_schema(),
            "stream": False,
        })
        self.token_usage += data.get("prompt_eval_count", 0) + data.get("eval_count", 0)
        try:
            return schema.model_validate(json.loads(data["message"]["content"]))
        except (json.JSONDecodeError, ValidationError, KeyError) as exc:
            raise LLMOutputError(f"Ollama 输出不符 schema: {exc}") from exc

    def health(self) -> bool:
        try:
            return httpx.get(f"{self.settings.ollama_base_url}/api/tags",
                             timeout=5).is_success
        except httpx.HTTPError:
            return False
