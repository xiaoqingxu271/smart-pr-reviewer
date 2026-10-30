"""结构化日志（PRD §6.1）：单行 key=value，便于日志系统直接检索聚合。

红线（PRD §6.4）：绝不记录 API key 等配置值，也不记录源码正文——只记计数、
分数与耗时。
"""
from __future__ import annotations

import logging

LOGGER_NAME = "pr_reviewer"


def configure(level: int = logging.INFO) -> None:
    root = logging.getLogger(LOGGER_NAME)
    if root.handlers:                    # 幂等：重复 import 不叠加 handler
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    root.handlers = [handler]
    root.setLevel(level)
    root.propagate = False


def get_logger() -> logging.Logger:
    configure()
    return logging.getLogger(LOGGER_NAME)
