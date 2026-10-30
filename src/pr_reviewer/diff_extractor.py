"""需求 1.1：Diff 提取与结构化（PRD §4）。

- 统一解析 unified diff 文本，产出 DiffResult（新文件绝对行号映射，PRD §4.2）
- 噪音过滤：锁文件 / vendored / 二进制（PRD §4.3）
- 本地 GitPython 模式；API 模式见 GithubPrSource / GitlabPrSource（M1 里程碑后续实现）
"""
from __future__ import annotations

import fnmatch
import re

from pr_reviewer.models import ChangeType, DiffResult, FileChange

HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
DIFF_FILE_HEADER = re.compile(r"^diff --git a/(.+) b/(.+)$")

# PRD §4.3 跳过清单（glob 匹配新文件路径）
SKIP_PATTERNS = [
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    "Pipfile.lock", "go.sum",
    "vendor/**", "third_party/**", "dist/**", "node_modules/**",
]
BINARY_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip",
                     ".gz", ".tar", ".woff", ".woff2", ".ttf", ".exe", ".dll",
                     ".pyc", ".pyo", ".so", ".dylib", ".class", ".jar", ".war",
                     ".wasm", ".o", ".a", ".lib", ".obj"}


def skip_reason_for(path: str) -> str | None:
    """返回噪音过滤的跳过原因；None 表示需要审查。"""
    if any(fnmatch.fnmatch(path, pat) for pat in SKIP_PATTERNS):
        return "lockfile/vendor/generated (skip patterns)"
    if path.rsplit(".", 1)[-1].lower() in {e.strip(".") for e in BINARY_EXTENSIONS}:
        return "binary/asset file"
    return None


def classify_change(diff_body: str) -> ChangeType:
    if diff_body.startswith("new file mode"):
        return "added"
    if diff_body.startswith("deleted file mode"):
        return "deleted"
    if "rename from" in diff_body or "similarity index" in diff_body:
        return "renamed"
    return "modified"


def file_change_from_patch(new_path: str, patch: str,
                           change_type: ChangeType = "modified") -> FileChange:
    """从单个文件的 patch 文本（含 @@ 头）构造 FileChange，供 API 模式复用。"""
    fc = FileChange(file_path=new_path, change_type=change_type,
                    skip_reason=skip_reason_for(new_path))
    new_line = 0
    in_hunk = False
    for line in patch.splitlines():
        hunk = HUNK_HEADER.match(line)
        if hunk:
            new_line = int(hunk.group(1))
            in_hunk = True
            continue
        if in_hunk:
            if line.startswith("\\"):
                # "\ No newline at end of file" 标记行，不占行号
                continue
            if line.startswith("+") and not line.startswith("+++"):
                fc.changed_lines.append(new_line)
                new_line += 1
            elif not (line.startswith("-") and not line.startswith("---")):
                new_line += 1
    return fc


def parse_unified_diff(diff_text: str) -> DiffResult:
    """解析 unified diff，输出每个文件的新文件绝对行号变更区间（PRD §4.2）。

    仅记录新增/上下文行为 changed_lines 中的【新增与修改行】（以 + 开头且非 +++）。
    """
    files: list[FileChange] = []
    current: FileChange | None = None
    new_line = 0
    in_hunk = False

    for line in diff_text.splitlines():
        header = DIFF_FILE_HEADER.match(line)
        if header:
            current = FileChange(
                file_path=header.group(2),
                change_type="modified",
                skip_reason=skip_reason_for(header.group(2)),
            )
            files.append(current)
            in_hunk = False
            continue

        if current is None:
            continue

        if line.startswith(("new file mode", "deleted file mode")) or line.startswith("rename from"):
            current.change_type = classify_change(line)

        hunk = HUNK_HEADER.match(line)
        if hunk:
            new_line = int(hunk.group(1))
            in_hunk = True
            continue

        if in_hunk:
            if line.startswith("\\"):
                # "\ No newline at end of file" 标记行，不占行号
                continue
            if line.startswith("+") and not line.startswith("+++"):
                current.changed_lines.append(new_line)
                new_line += 1
            elif line.startswith("-") and not line.startswith("---"):
                pass  # 删除行无新行号，归入邻近区间（PRD §4.2）
            else:
                new_line += 1

    total = sum(len(f.changed_lines) for f in files)
    return DiffResult(files=files, raw_diff=diff_text, total_changed_lines=total)


def extract_local_diff(repo_path: str, base_branch: str, head_branch: str) -> DiffResult:
    """本地仓库模式（PRD §4.1）：GitPython 读取 base...head 三点 diff。"""
    import git  # 延迟导入，避免无 git 环境时影响其他模块

    repo = git.Repo(repo_path)
    diff_text = repo.git.diff(f"{base_branch}...{head_branch}", unified=3)
    return parse_unified_diff(diff_text)
