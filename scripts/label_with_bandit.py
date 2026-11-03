"""用 Bandit 在**真实代码**上生成检索评测的标注（不写任何合成用例）。

为什么需要这一步：语料换成真的 CWE 只解决"规则文本是真的"，不解决
"这段代码该召回哪条规则"。后者如果还是我编的，放大到 1000 条只是把欺骗性放大。
Bandit 是独立第三方静态分析器，它的每条 finding 都自带 CWE 编号
（`issue_cwe.id`，官方映射），于是 (真实代码片段 → 期望条款) 这对标注不由我产生。

用法：
    uv run python scripts/label_with_bandit.py --roots "D:/Code/Practical Project" ...
产物：data/labels/bandit_findings.json —— 原始路径、行号、CWE 编号、bandit 自述、
     以及 enclosing 函数体（真正的查询文本在这里，见 bench_retrieval_scale.py）。

刻意不做的事：不把 bandit 的 issue_text 当查询——那等于让它自己检索自己的描述，
分数会虚高到没有意义。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "data" / "labels" / "bandit_findings.json"

# 只扫真实源码：虚拟环境、依赖目录、构建产物里的代码不是"我们的工程师写的代码"
EXCLUDE_PARTS = {".venv", "venv", "env", "node_modules", "site-packages", "__pycache__",
                 ".git", ".idea", "dist", "build", "migrations", "alembic", "test", "tests"}


def collect(roots: list[Path], limit: int | None) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        for p in sorted(root.rglob("*.py")):
            parts = set(p.parts)
            if parts & EXCLUDE_PARTS:
                continue
            if not p.is_file() or p.stat().st_size > 200_000:
                continue
            files.append(p)
    files.sort()
    return files[:limit] if limit else files


def run_bandit(batch: list[Path]) -> list[dict]:
    """bandit 一次只吃一小批：Windows 命令行有长度上限，分批后合并。"""
    proc = subprocess.run(
        [sys.executable, "-m", "bandit", "-q", "-f", "json", *[str(p) for p in batch]],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    # bandit 发现问题时退出码非 0，这不是失败
    payload = proc.stdout.strip()
    if not payload:
        raise RuntimeError(f"bandit 无输出（stderr 前 200 字）：{proc.stderr[:200]}")
    return json.loads(payload).get("results", [])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--roots", nargs="+", required=True, help="要扫的真实代码目录")
    ap.add_argument("--limit", type=int, default=None, help="最多取多少个文件（调试用）")
    ap.add_argument("--batch", type=int, default=150, help="每次 bandit 调用带多少个文件")
    args = ap.parse_args()

    roots = [Path(r).resolve() for r in args.roots]
    missing = [str(r) for r in roots if not r.exists()]
    if missing:
        print(f"!! 目录不存在：{missing}", file=sys.stderr)
        return 2

    files = collect(roots, args.limit)
    print(f"真实源码文件 {len(files)} 个（已排除虚拟环境/依赖/构建目录）")

    findings: list[dict] = []
    for i in range(0, len(files), args.batch):
        batch = files[i:i + args.batch]
        got = run_bandit(batch)
        findings.extend(got)
        print(f"  批次 {i // args.batch + 1}：{len(batch)} 文件 → 累计 findings {len(findings)}")

    for f in findings:
        f["cwe_id"] = (f.get("issue_cwe") or {}).get("id")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"files_scanned": len(files),
                               "roots": [str(r) for r in roots],
                               "bandit_version": subprocess.run(
                                   [sys.executable, "-m", "bandit", "--version"],
                                   capture_output=True, text=True).stdout.strip(),
                               "findings": findings},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    cwes = sorted({f["cwe_id"] for f in findings if f.get("cwe_id") is not None})
    print(f"\nfindings={len(findings)}  涉及 {len(cwes)} 个不同 CWE 编号 → {OUT}")
    print("CWE 分布（前 15）:", {c: sum(1 for f in findings if f['cwe_id'] == c)
                                 for c in cwes[:15]})
    return 0


if __name__ == "__main__":
    sys.exit(main())
