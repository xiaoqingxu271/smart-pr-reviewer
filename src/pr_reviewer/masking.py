"""敏感值脱敏（PRD §6.4）：密钥类值只保留前缀，其余打码。

放在结果构造层而不是只在报告渲染层：`GET /review/{id}` 返回的 JSON 同样是对外
输出，只在 Markdown 里打码等于换个出口就把原文交出去了。
"""
from __future__ import annotations

import re

_SECRET_RE = re.compile(r"(sk-[A-Za-z0-9_-]{4})([A-Za-z0-9_-]+)")
# 引号包裹的长 token（sup3r-s3cret-key 等），保留前 4 位
_QUOTED_SECRET_RE = re.compile(r"(['\"])([A-Za-z0-9]{4})[A-Za-z0-9_-]{8,}\1")


def mask_secrets(text: str) -> str:
    if not text:
        return text
    text = _SECRET_RE.sub(r"\1****", text)
    return _QUOTED_SECRET_RE.sub(r"\1\2****\1", text)
