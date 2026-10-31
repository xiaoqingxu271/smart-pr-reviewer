"""Webhook 入口：HMAC 验签 + 事件解析（PRD §6.1）。

两个必须注意的点：
- 验签要用**原始请求字节**。先 JSON 解析再重序列化会因键序/空白变化而签名对不上；
  而"未验签就先解析 body"等于把"任何人/任何系统都能触发对本机可见仓库的审查并
  让结果写回 PR"的权限开放出去。
- webhook 端点的鉴权靠 HMAC 签名，不走 Bearer token：GitHub 不会带你的 API token，
  但这不代表它可以裸奔——没配 `WEBHOOK_SECRET` 时该端点直接 503 停用（fail-closed）。
"""
from __future__ import annotations

import hashlib
import hmac
import json

from pr_reviewer.models import ReviewRequest

# 只有这些动作意味着"PR 内容变了，需要重新审"
REVIEW_ACTIONS = {"opened", "synchronize", "reopened"}


def verify_signature(secret: str, payload: bytes, header_value: str | None) -> bool:
    """校验 GitHub 的 `X-Hub-Signature-256: sha256=<hex>`。

    用 `hmac.compare_digest` 而不是 `==`：签名比较要走常量时间，
    否则就是在给攻击者一个逐字节猜签名的侧信道。
    """
    if not secret or not header_value:
        return False
    scheme, _, provided = header_value.partition("=")
    if scheme.strip().lower() != "sha256":
        return False
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, provided.strip().lower())


def parse_pull_request_event(event: str | None, body: dict) -> ReviewRequest | None:
    """把 pull_request 事件翻成审查请求；不关心的事件返回 None。"""
    if event != "pull_request" or not isinstance(body, dict):
        return None
    if body.get("action") not in REVIEW_ACTIONS:
        return None
    pr = body.get("pull_request") or {}
    repo_url = (body.get("repository") or {}).get("html_url") or ""
    number = pr.get("number")
    if not repo_url or not number:
        return None
    return ReviewRequest(mode="api", repo_url=repo_url, pr_number=int(number),
                         post_comments=True)


class WebhookRejected(ValueError):
    """签名缺失/不匹配：调用方不可信 → 403。"""


class WebhookPayloadInvalid(ValueError):
    """签名有效但内容解析不了（最常见是 Content type 配成了 form-urlencoded）→ 400。

    这两类必须分开报：都返回"签名校验失败"会把人引向去查密钥，
    而真正的问题是仓库 webhook 设置里的 Content type。
    """


def parse_webhook(secret: str, raw: bytes, signature: str | None,
                  event: str | None) -> ReviewRequest | None:
    """验签 → 解析。签名不对抛 WebhookRejected，内容坏抛 WebhookPayloadInvalid。"""
    if not verify_signature(secret, raw, signature):
        raise WebhookRejected("invalid webhook signature")
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise WebhookPayloadInvalid(
            "webhook payload 不是合法 JSON（检查 GitHub webhook 的 Content type "
            "是否为 application/json）") from exc
    return parse_pull_request_event(event, body)
