"""出站 HTTPS 的信任库策略（所有 httpx / OpenAI SDK 客户端共用）。

为什么需要这个模块：Python 里有两套互不相通的信任库。标准库 `ssl`（以及 urllib、浏览器、
Windows 上的 git）认**操作系统信任库**，而 httpx 默认只认 **certifi 自带的 CA 包**。
本机实测就是这个裂缝：用 Watt Toolkit / Steam++ 这类"GitHub 加速"时，`api.github.com`
的证书由本地代理签发，它的根证书只装进了 Windows 证书存储——于是同一个请求
urllib 通、httpx 挂，报 `CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate`。
企业内网的 TLS 审计代理（Zscaler / Fortinet / 深信服）是完全一样的形态，
所以这不是"本机环境脏"，而是内网部署必踩的坑。

做法是把两个信任源**取并集**：仍然要求证书链验到某个受信根、仍然校验域名，
只是"受信根"的集合与浏览器一致。绝不用 `verify=False`——那等于把 GitHub 令牌
发给任何能做中间人的人。

Linux / macOS 上 `create_default_context()` 读的是系统 bundle，运维把内网根证书
`update-ca-certificates` 一下即可，不需要改代码。
"""
from __future__ import annotations

import functools
import ssl

import certifi


@functools.lru_cache(maxsize=1)
def ssl_context() -> ssl.SSLContext:
    """OS 信任库 ∪ certifi，验证强度不降。

    缓存是必要的：Windows 上枚举证书存储要读注册表，每次建连接都做一遍会拖慢请求。
    `load_verify_locations` 是**追加**而不是替换，所以两次调用得到并集。
    """
    ctx = ssl.create_default_context()                  # 操作系统信任库
    ctx.load_verify_locations(cafile=certifi.where())   # 并上 certifi 的公共 CA 包
    return ctx
