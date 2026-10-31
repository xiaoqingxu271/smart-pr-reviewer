"""Webhook 验签与事件解析（PRD §6.1）。

签名校验的常见错误：验签前先解析 JSON（给了未验证内容执行路径）、用 `==` 比签名
（时序侧信道）、拿重序列化后的字节去验（键序/空白一变就永远验不过）。
"""
import hashlib
import hmac
import json

import pytest

from pr_reviewer.webhooks import parse_pull_request_event, parse_webhook, verify_signature

SECRET = "s3cret-webhook"


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def event_body(action: str = "opened", repo: str = "https://github.com/acme/api") -> bytes:
    return json.dumps({"action": action, "repository": {"html_url": repo},
                       "pull_request": {"number": 7}}).encode()


def test_valid_signature_and_tamper_detection():
    body = event_body()
    assert verify_signature(SECRET, body, sign(body))
    assert not verify_signature(SECRET, event_body("closed"), sign(body))  # 内容被换
    assert not verify_signature(SECRET, body, "sha256=" + "0" * 64)       # 签名不对
    assert not verify_signature(SECRET, body, None)                        # 缺头
    assert not verify_signature(SECRET, body, "sha1=" + "0" * 40)          # 算法不符
    assert not verify_signature("", body, sign(body))                      # 未配密钥 = 不通过


def test_signature_comparison_is_not_plain_equality():
    """长度不等的签名必须直接失败，而不是抛异常或走到比较之外。"""
    assert verify_signature(SECRET, event_body(), "sha256=abc") is False


def test_only_pr_content_events_are_reviewed():
    req = parse_pull_request_event("pull_request", json.loads(event_body()))
    assert req is not None and req.repo_url.endswith("acme/api")
    assert req.pr_number == 7 and req.post_comments is True

    for action in ("closed", "labeled", "assigned"):
        assert parse_pull_request_event("pull_request",
                                        json.loads(event_body(action))) is None
    for event in ("push", "ping", None):
        assert parse_pull_request_event(event, json.loads(event_body())) is None
    assert parse_pull_request_event("pull_request", {"action": "opened"}) is None  # 缺仓库信息


def test_parse_webhook_rejects_unverified_and_malformed():
    body = event_body()
    with pytest.raises(ValueError, match="signature"):
        parse_webhook(SECRET, body, "sha256=bad", "pull_request")
    with pytest.raises(ValueError, match="JSON"):
        parse_webhook(SECRET, b"{not json", sign(b"{not json"), "pull_request")
    assert parse_webhook(SECRET, body, sign(body), "ping") is None
