"""需求 1.2：基于代码语法的智能分块（PRD §5.2）。

三级降级链：
  1. Python: ast 模块（函数/类完整边界，装饰器计入起始行）
  2. 其他代码文件: 函数/类边界正则（JS/TS/Java/Go/SQL 等）
  3. 兜底: 按行切 + degraded=True（报告中声明可信度降级）

只送审包含变更行的块，由 pipeline 层（pipeline.py）过滤；本模块负责切块本身。
"""
from __future__ import annotations

import ast
import re

from pr_reviewer.models import CodeChunk

# 二级降级：函数/类边界启发式正则（覆盖 JS/TS/Java/Go/C 系）
BOUNDARY_RE = re.compile(
    r"^\s*"                                 # 缩进
    r"(?:export\s+|public\s+|private\s+|protected\s+|static\s+|async\s+|func\s+)*"
    r"(?:function\s+\w+|class\s+\w+|def\s+\w+"
    r"|(?:\w+(?:<[^>]*>)?\s+)?\w+\s*\([^)]*\)\s*\{)",  # C/Java/Go 方法签名
    re.MULTILINE,
)

LINE_WINDOW = 100       # 兜底按行切窗口
LINE_OVERLAP = 50


def chunk_file(file_path: str, content: str, changed_lines: list[int]) -> list[CodeChunk]:
    """按语言选择分块策略（PRD §5.2 降级链入口）。"""
    lang = file_path.rsplit(".", 1)[-1].lower() if "." in file_path else ""
    if lang == "py":
        chunks = _chunk_python(file_path, content)
        if chunks:
            return _attach(chunks, changed_lines, lang)
        # ast.parse 失败（PR 代码本身可能有语法错误）→ 降级到正则方案
        chunks = _chunk_regex(file_path, content)
        if chunks:
            return _attach([c.model_copy(update={"degraded": True}) for c in chunks],
                           changed_lines, lang)
    elif lang in {"js", "ts", "jsx", "tsx", "java", "go", "c", "cpp", "h", "sql", "sh"}:
        chunks = _chunk_regex(file_path, content)
        if chunks:
            return _attach(chunks, changed_lines, lang)
    # 兜底：按行切
    return _attach(_chunk_lines(file_path, content), changed_lines, lang)


def _chunk_python(file_path: str, content: str) -> list[CodeChunk]:
    """AST 分块：顶层与类内函数/类为块单位，装饰器行计入起始行。"""
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return []

    lines = content.splitlines()
    chunks: list[CodeChunk] = []

    def walk(body, prefix: str):
        for node in body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            start = min([node.lineno, *[d.lineno for d in node.decorator_list]]
                        if node.decorator_list else [node.lineno])
            end = node.end_lineno or node.lineno
            symbol = f"{prefix}{node.name}"
            if isinstance(node, ast.ClassDef):
                # 类头（含 docstring/字段）作为一块，类内方法单独成块
                body_end = _class_header_end(node)
                chunks.append(CodeChunk(
                    file_path=file_path, start_line=start, end_line=body_end,
                    symbol=symbol, language="py",
                    content="\n".join(lines[start - 1:body_end]),
                ))
                walk(node.body, prefix=f"{symbol}.")
            else:
                chunks.append(CodeChunk(
                    file_path=file_path, start_line=start, end_line=end,
                    symbol=symbol, language="py",
                    content="\n".join(lines[start - 1:end]),
                ))

    walk(tree.body, prefix="")
    # 顶层 import/常量归为 <module>" 块（类体非方法语句已含在类头块中，不重复处理）
    _module_level_chunks(file_path, tree, lines, chunks)
    chunks.sort(key=lambda c: c.start_line)
    return chunks


def _class_header_end(node: ast.ClassDef) -> int:
    """类头块 = 类定义行到第一个方法之前的行。"""
    for child in node.body:
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return max(child.lineno - 1, node.lineno)
    return node.end_lineno or node.lineno


def _module_level_chunks(file_path: str, tree: ast.Module, lines: list[str],
                         chunks: list[CodeChunk]) -> None:
    """顶层 import / 常量 / 表达式归入 <module> 块（按连续区间聚合）。"""
    others = [n for n in tree.body
              if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    if not others:
        return
    spans = sorted((n.lineno, n.end_lineno or n.lineno) for n in others)
    # 聚合连续区间（间隔 <= 2 视为同块）
    groups: list[list[int]] = [list(spans[0])]
    for s, e in spans[1:]:
        if s - groups[-1][1] <= 2:
            groups[-1][1] = e
        else:
            groups.append([s, e])
    for s, e in groups:
        chunks.append(CodeChunk(
            file_path=file_path, start_line=s, end_line=e,
            symbol="<module>", language="py",
            content="\n".join(lines[s - 1:e]),
        ))


def _chunk_regex(file_path: str, content: str) -> list[CodeChunk]:
    """二级降级：函数/类边界正则切块。"""
    lines = content.splitlines()
    starts = [m.start() for m in BOUNDARY_RE.finditer(content)]
    if not starts:
        return []
    start_lines = [content.count("\n", 0, s) + 1 for s in starts]
    start_lines.append(len(lines) + 1)  # 哨兵
    chunks = []
    for i in range(len(start_lines) - 1):
        s, e = start_lines[i], start_lines[i + 1] - 1
        symbol = lines[s - 1].strip()[:80]
        chunks.append(CodeChunk(
            file_path=file_path, start_line=s, end_line=e,
            symbol=symbol, language="regex",
            content="\n".join(lines[s - 1:e]),
        ))
    return chunks


def _chunk_lines(file_path: str, content: str) -> list[CodeChunk]:
    """三级兜底：按行窗口 + 重叠切分，标记 degraded。"""
    lines = content.splitlines()
    chunks = []
    for s in range(0, len(lines), LINE_WINDOW - LINE_OVERLAP):
        window = lines[s:s + LINE_WINDOW]
        if not window:
            break
        chunks.append(CodeChunk(
            file_path=file_path, start_line=s + 1, end_line=s + len(window),
            symbol=f"<lines {s + 1}-{s + len(window)}>", language="plain",
            content="\n".join(window), degraded=True,
        ))
    return chunks


def _attach(chunks: list[CodeChunk], changed_lines: list[int], lang: str) -> list[CodeChunk]:
    """填充每个块的 changed_lines（块内发生变更的行），供 pipeline 过滤送审范围。"""
    result = []
    for c in chunks:
        hit = [ln for ln in changed_lines if c.start_line <= ln <= c.end_line]
        result.append(c.model_copy(update={"changed_lines": hit}))
    return result


def split_oversized_chunk(chunk: CodeChunk, token_limit: int) -> list[CodeChunk]:
    """巨函数二级切分（PRD §5.2）：按顶层语句分组聚合，函数签名头作为每块前缀。

    token_limit 以字符数近似（1 token ≈ 3 字符）。
    """
    approx_chars = token_limit * 3
    if len(chunk.content) <= approx_chars:
        return [chunk]
    try:
        tree = ast.parse(chunk.content)
    except SyntaxError:
        return _chunk_lines(chunk.file_path, chunk.content)

    # chunk 内容是单个函数/类：取其内部语句；否则取顶层语句
    if (len(tree.body) == 1
            and isinstance(tree.body[0], (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and hasattr(tree.body[0], "body")):
        stmts = tree.body[0].body
    else:
        stmts = tree.body

    lines = chunk.content.splitlines()
    header = lines[:1]  # 函数签名行，作为每块前缀
    budget = approx_chars - len("\n".join(header))
    # 按语句的实际字符数累计分组，每组 ≤ budget
    groups: list[tuple[int, int]] = []
    cur_start, cur_end, cur_size = None, None, 0
    for node in stmts:
        s, e = node.lineno, node.end_lineno or node.lineno
        size = len("\n".join(lines[s - 1:e])) + 1
        if cur_start is not None and cur_size + size > budget:
            groups.append((cur_start, cur_end))
            cur_start, cur_size = None, 0
        cur_start = s if cur_start is None else cur_start
        cur_end, cur_size = e, cur_size + size
    if cur_start is not None:
        groups.append((cur_start, cur_end))

    return [
        CodeChunk(
            file_path=chunk.file_path,
            start_line=chunk.start_line + s - 1,
            end_line=chunk.start_line + e - 1,
            symbol=chunk.symbol, language=chunk.language,
            content="\n".join(header + lines[s - 1:e]),  # 签名行前缀
            degraded=True,
        )
        for s, e in groups
    ]
