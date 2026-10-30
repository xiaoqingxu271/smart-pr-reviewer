"""离线步骤：构建规范向量索引（PRD §5.3）。用法：

    python -m scripts.build_standards_index [--md docs/研发代码安全规范.md]
"""
from __future__ import annotations

import argparse

from pr_reviewer.config import get_settings
from pr_reviewer.rag import StandardsRetriever


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--md", default=str(get_settings().standards_md_path))
    args = parser.parse_args()
    retriever = StandardsRetriever(get_settings())
    count = retriever.build_index(args.md)  # TODO(M3)
    print(f"indexed {count} rule chunks")


if __name__ == "__main__":
    main()
