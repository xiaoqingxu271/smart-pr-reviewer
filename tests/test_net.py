"""出站信任库策略（net.py）：验证不关，但要把"操作系统信任库"一起算进来。

真实故障复盘：本机装了 GitHub 加速类工具（本地代理签发 api.github.com 的证书，根证书只进
Windows 证书存储），httpx 默认只认 certifi → 审查任务在取 diff 时就 `CERTIFICATE_VERIFY_FAILED`，
对外只表现为"任务 failed + 一句友好文案"。这类坑在企业内网 TLS 审计代理下一模一样。
"""
import ssl

import certifi
import httpx

from pr_reviewer.config import Settings
from pr_reviewer.net import ssl_context


def subjects(ctx: ssl.SSLContext) -> set:
    return {next(iter(c["subject"])) for c in ctx.get_ca_certs()}


def test_context_keeps_verification_on():
    ctx = ssl_context()
    assert ctx.verify_mode is ssl.CERT_REQUIRED
    assert ctx.check_hostname is True          # 只并信任源，绝不退化成 verify=False


def test_context_is_union_not_replacement():
    """并集：certifi 里的公共 CA 一个都不能少，否则换环境就换一批连不上。"""
    merged = subjects(ssl_context())
    only_certifi = subjects(ssl.create_default_context(cafile=certifi.where()))
    assert only_certifi <= merged
    # 操作系统信任库通常与 certifi 不完全重合（Windows 存储里有本机装的根）
    assert merged and len(merged) >= len(only_certifi)


def test_context_is_cached():
    assert ssl_context() is ssl_context()      # Windows 枚举证书存储不便宜，不能每请求做一次


def test_http_clients_use_it(monkeypatch):
    """取数与写回两条出站链路必须共用同一策略，漏一处就是漏一个内网部署的坑。"""
    import pr_reviewer.publishing as publishing
    import pr_reviewer.sources as sources
    seen: list[object] = []
    real = httpx.Client

    def spy(*args, **kwargs):
        seen.append(kwargs.get("verify"))
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", spy)
    settings = Settings(_env_file=None, github_token="t")
    sources._github_client(settings).close()
    gitlab_client, _base = sources._gitlab_client()
    gitlab_client.close()
    publishing.GitHubPublisher("t")._http().close()
    assert seen and all(v is seen[0] for v in seen), seen
    assert seen[0] is ssl_context()


def test_llm_sdk_carries_the_same_context():
    """LLM 侧单独验：openai SDK 3.x 依赖的是 httpx2 这个分叉，传 httpx.Client 类型不通。

    这里不替换 `httpx2.Client` 来断言——SDK 在建 client 时会对它做 isinstance 检查，
    替换成函数会让构造直接崩；改成读它实际装进连接池的 context，断言的是同一个对象。
    """
    from pr_reviewer.llm.openai_backend import OpenAIBackend

    backend = OpenAIBackend(Settings(_env_file=None, openai_api_key="k"))
    pool_ctx = backend._ensure_client()._client._transport._pool._ssl_context
    assert pool_ctx is ssl_context()
