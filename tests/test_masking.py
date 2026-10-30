"""脱敏行为单元测试（PRD §6.4）：锁定"保留 sk- + 4 位，其余打码"的口径。

之前脱敏只在 Markdown 渲染层生效，JSON 响应仍回显原文；这个测试顺带把口径钉住，
避免以后改正则时悄悄多露几位。
"""
from pr_reviewer.masking import mask_secrets


def test_masks_openai_style_key_to_prefix_four():
    assert mask_secrets("key sk-abcdef123456 found") == "key sk-abcd**** found"


def test_masks_quoted_long_token():
    out = mask_secrets('password = "sup3r-s3cret-key-abc"')
    assert out == 'password = "sup3****"'          # 保留前 4 位，其余打码
    assert "s3cret-key-abc" not in out


def test_leaves_ordinary_text_alone():
    assert mask_secrets("SQL 查询使用了 f-string 拼接") == "SQL 查询使用了 f-string 拼接"
    assert mask_secrets("") == ""
