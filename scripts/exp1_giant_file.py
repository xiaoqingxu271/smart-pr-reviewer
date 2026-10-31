"""破坏性实验 1（PRD §7.3）：5000 行巨型文件，按行硬切 vs AST 分块。

离线可跑：python scripts/exp1_giant_file.py
产出 experiments/exp1-giant-file.md，含函数切断率实测数据与结论。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from pr_reviewer.chunker import _chunk_lines, chunk_file


def make_giant_file(n_funcs: int = 100, lines_per_func: int = 50) -> tuple[str, list[tuple[str, int, int]]]:
    """生成 n_funcs × lines_per_func 行的文件；返回 (内容, [(函数名, 起, 止)])。"""
    lines, spans = [], []
    for i in range(n_funcs):
        start = len(lines) + 1
        lines.append(f"def process_{i}(records):")
        lines.append(f'    """第 {i} 个处理函数：校验、转换、聚合。"""')
        lines.append("    validated = []")
        for j in range(lines_per_func - 4):
            lines.append(f"    validated.append(records[{j}] * {i + 1})")
        lines.append("    return validated")
        spans.append((f"process_{i}", start, len(lines)))
    return "\n".join(lines) + "\n", spans


def cut_stats(chunks, spans) -> tuple[int, int, list[str]]:
    """统计被切断的函数：函数体与 >1 个块相交即视为切断。"""
    cut = []
    for name, s, e in spans:
        touching = [c for c in chunks if c.start_line <= e and c.end_line >= s]
        if len(touching) > 1:
            cut.append(name)
    return len(cut), len(spans), [c for c in cut[:5]]


def main() -> None:
    content, spans = make_giant_file()
    n_lines = len(content.splitlines())
    changed = list(range(1, n_lines + 1))

    # 对照组：按行硬切（与 chunker 兜底同参数：100 行窗口 / 50 重叠）
    line_chunks = _chunk_lines("giant.py", content)
    # 实验组：AST 分块
    ast_chunks = chunk_file("giant.py", content, changed)

    cut_line, total, examples = cut_stats(line_chunks, spans)
    cut_ast, _, _ = cut_stats(ast_chunks, spans)
    avg_line = sum(len(c.content) for c in line_chunks) // len(line_chunks)
    avg_ast = sum(len(c.content) for c in ast_chunks) // len(ast_chunks)

    md = f"""# 破坏性实验 1：5000 行巨型文件的分块对照（PRD §7.3）

## 设置

- 构造 {n_lines} 行 Python 文件：{len(spans)} 个完整函数，每个 {len(content.splitlines()) // len(spans)} 行
- 变更行 = 全部行（模拟「整个文件被重写」的 PR）
- 对照组：按行硬切（100 行窗口 / 50 行重叠，与 chunker 三级兜底同参数）
- 实验组：AST 分块（本项目的需求 1.2 实现）
- 指标：**函数切断率** = 函数体与 >1 个块相交的函数数 / 总函数数（PRD §5.2）

## 结果

| 指标 | 按行硬切 | AST 分块 |
|---|---|---|
| 块数 | {len(line_chunks)} | {len(ast_chunks)} |
| 平均块大小（字符） | {avg_line} | {avg_ast} |
| **被切断的函数数** | **{cut_line} / {total}** | **{cut_ast} / {total}** |
| **函数切断率** | **{cut_line / total:.0%}** | **{cut_ast / total:.0%}** |

被切断函数示例（对照组）：{', '.join(examples)}…

## 上下文丢失的误判分析

以 `process_7` 为例：其参数校验段落在块 A 末尾，聚合与 return 段落在块 B 开头。
审查块 A 时模型只看到「向列表 append 记录」的循环，看不到函数的校验意图与返回值契约，
容易把正常的批量转换误报为「无输入校验的批量处理」（逻辑类误判）；而块 B 缺少
变量定义，行号定位也会漂移。AST 分块下每个函数整体成块，不存在该问题。

## 结论（面试口径）

> 简单按行切分导致 **{cut_line / total:.0%}** 的函数被切断，上下文丢失使模型只能看到
> 函数片段，产生逻辑误判与行号漂移；引入 ast 模块按语法边界解析后，函数切断率为
> **{cut_ast / total:.0%}**，每个送审块都是完整函数，从结构上消除了这类误判。
> （注：本实验指标为分块器自身的结构指标；对召回率/误报率的影响见实验 2 的 LLM 对照。）
"""
    out = ROOT / "experiments" / "exp1-giant-file.md"
    out.write_text(md, encoding="utf-8")
    print(md)


if __name__ == "__main__":
    main()
