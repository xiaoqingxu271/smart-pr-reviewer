"""配置管理（PRD §6.2）：.env + Pydantic Settings。"""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # LLM 后端（PRD §6.4：openai 意味着源码出内网）
    llm_backend: str = "openai"
    model_name: str = "gpt-4o-mini"
    openai_api_key: str = ""
    openai_base_url: str = ""
    ollama_base_url: str = "http://localhost:11434"

    # LLM 调用容错（PRD §6.3：限流/超时指数退避重试；仍失败按块计 review_error，
    # 不中断整份 PR——429 冒泡会让整个任务作废，实测发生过）
    llm_timeout_seconds: int = 60
    ollama_timeout_seconds: int = 300   # 本地 CPU 推理慢，需更长超时
    llm_max_retries: int = 3

    # Embedding（PRD §5.3 禁止 Chroma 默认 MiniLM；选型与阈值实证见
    # experiments/rag-embedding-bench.md：bge-base-zh-v1.5 + query 指令前缀 R@1=1.00）
    embedding_backend: str = "local"      # local | ollama | openai | hash
    embedding_model: str = "BAAI/bge-base-zh-v1.5"
    # BGE-zh 官方的非对称检索指令，只加在 query 侧（doc 侧不加）。
    # 换用不需要指令的模型（如 bge-m3）时置空。
    embedding_query_prefix: str = "为这个句子生成表示以用于检索相关文章："

    # 审查参数
    token_budget: int = 100_000
    chunk_token_limit: int = 3000       # 巨函数二级切分阈值（PRD §5.2）
    retrieval_top_k: int = 3
    # 检索过滤（PRD §5.3）：threshold 判 top1——"这次检索到底有没有指向"；
    # margin 判尾部——"还有哪些条款和冠军属于同一个答案"。
    # 0.45/0.05 是「条款带典型违规示例 + bge-base-zh-v1.5 + 检索前缀」在 10 个漏洞块 /
    # 15 个干净块上的标定值（正确条款 min 0.460，干净块 top1 median 0.463）。
    # 注意 0.45 与最弱正确条款只差 0.01：改规范文档或换 embedding 模型都必须重跑
    # scripts/bench_rag_embeddings.py 重标定，并用 scripts/check_retrieval.py 复核。
    retrieval_score_threshold: float = 0.45
    retrieval_margin: float = 0.05
    review_max_retries: int = 2
    cache_ttl_hours: int = 24

    # 服务
    host: str = "0.0.0.0"
    port: int = 8000
    # 接口鉴权（PRD §6.4）：留空 = 审查接口一律 503 停用（fail-closed），
    # 而不是"没配就当公开服务跑"——报告里含源码片段，裸奔等于源码外泄通道
    api_token: str = ""
    # local 模式允许读取的仓库根目录（逗号分隔）；留空 = 禁用 local 模式。
    # 不限制的话任何调用方都能让服务器读取磁盘上任意 git 仓库
    allowed_repo_roots: str = ""

    # 路径
    standards_md_path: Path = Path("docs/研发代码安全规范.md")
    chroma_dir: Path = Path(".chroma")
    reports_dir: Path = Path("reports")
    db_path: Path = Path(".data/reviews.sqlite3")   # 任务态 + 结果缓存（PRD §6.1/§6.5）


def get_settings() -> Settings:
    return Settings()
