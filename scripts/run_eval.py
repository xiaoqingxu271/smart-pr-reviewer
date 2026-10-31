"""评测脚本（PRD §7.1/§7.2）：漏洞召回率 + 干净 PR 误报率。

用法：
    python scripts/run_eval.py --llm fake                     # 离线冒烟（确定性）
    python scripts/run_eval.py --llm real [--no-rag]          # 真实 LLM（读 .env）
    python scripts/run_eval.py --llm real --out experiments/eval-results.md

召回匹配规则（PRD §7.1）：文件相同 ∧ 行号差 ≤ 3 ∧ 类型等价
（security 类且描述命中类型关键词表）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from pr_reviewer.config import Settings
from pr_reviewer.diff_extractor import DiffResult
from pr_reviewer.models import FileChange
from pr_reviewer.pipeline import run_review

FIXTURES = ROOT / "tests" / "fixtures"
GT_PATH = ROOT / "tests" / "ground_truth.json"

# 期望漏洞类型 → issue 描述需命中的关键词（中英文）
TYPE_KEYWORDS = {
    "hardcoded_secret": ["secret", "key", "password", "token", "密钥", "密码", "硬编码"],
    "sql_injection": ["sql", "注入"],
    "command_injection": ["shell", "command", "命令", "注入"],
    "unsafe_deserialization": ["deserial", "pickle", "yaml", "反序列化"],
    "path_traversal": ["traversal", "path", "路径", "穿越"],
    "weak_hash": ["md5", "sha1", "weak", "弱哈希"],
    "tls_verification_disabled": ["verify", "tls", "证书", "校验"],
    "code_injection": ["eval", "exec", "注入", "执行"],
    "xss": ["xss", "innerhtml", "脚本", "跨站"],
    # 私有约定类：关键词只认「封装层的专有名字」。
    # 不能用 route / 连接 / 内部 这类通用词——模型描述问题时常直接引用代码行
    # （"def ... @router.get(...)"），于是无 RAG 也能被关键词判成命中，
    # 那是判据太松，不是模型本事。
    "route_not_registered": ["gateway"],
    "db_access_outside_repository": ["repository", "仓储", "dao"],
    "internal_call_without_client": ["internal_client"],
    "task_not_registered": ["scheduler"],
    "money_as_float": ["money"],
    "raw_exception_leaked": ["raise_business", "业务错误码"],
    "cachekey_concatenated": ["cachekey"],
}

# 私有约定类违规：判定只看位置与描述关键词，不要求 issue_type == "security"
# （模型完全可能合理地把它标成 style/logic，用类型过滤会系统性低估召回）
CONVENTION_TYPES = {"route_not_registered", "db_access_outside_repository",
                    "internal_call_without_client", "task_not_registered",
                    "money_as_float", "raw_exception_leaked",
                    "cachekey_concatenated"}


def load_pr_dir(pr_dir: Path) -> tuple[DiffResult, dict[str, str]]:
    """目录即一个 PR：所有文件视为新增（全行变更）。

    非 UTF-8 文本文件跳过（fixture 目录里混进过 pytest 生成的 __pycache__/*.pyc，
    无条件 read_text 会让整轮评测崩）；与生产侧 diff_extractor 的二进制跳过同口径。
    """
    files, contents = [], {}
    for p in sorted(pr_dir.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(pr_dir).as_posix()
        try:
            content = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            print(f"!! 跳过非文本 fixture 文件：{rel}", file=sys.stderr)
            continue
        files.append(FileChange(file_path=rel, change_type="added",
                                changed_lines=list(range(1, len(content.splitlines()) + 1))))
        contents[rel] = content
    return DiffResult(files=files), contents


class KeywordFakeLLM:
    """离线确定性后端：按正则扫描代码块产出 issue，用于验证评测链路。

    它模拟「一个能发现已知模式、且不误报正常代码」的理想审查员，
    fake 模式的结果代表评测框架自洽，不代表模型真实水平。
    """

    RULES = (
        ("hardcoded_secret", re.compile(r"['\"](?:sk-|sup3r|passw|token|secret)[^'\"]{4,}['\"]", re.I)),
        ("sql_injection", re.compile(r"SELECT .*\{|f\"SELECT|% .*SELECT|\+ .*SELECT", re.I)),
        ("command_injection", re.compile(r"shell\s*=\s*True")),
        ("unsafe_deserialization", re.compile(r"pickle\.loads|yaml\.load\(")),
        ("path_traversal", re.compile(r"os\.path\.join\([^)]*filename")),
        ("weak_hash", re.compile(r"hashlib\.(md5|sha1)\(")),
        ("tls_verification_disabled", re.compile(r"verify\s*=\s*False")),
        ("code_injection", re.compile(r"eval\(")),
        ("xss", re.compile(r"innerHTML\s*=")),
    )

    def __init__(self):
        self.token_usage = 0

    def chat_structured(self, system, user, schema):
        self.token_usage += 100
        if schema.__name__ == "SummaryOutput":
            return schema.model_validate({"summary": "（fake 模式摘要）"})
        issues = []
        file_path = re.search(r"待审查文件：(.+)", user).group(1).strip()
        start_line = int(re.search(r"块内第 1 行 = 文件第 (\d+) 行", user).group(1))
        body = user.split("<code>")[1].split("</code>")[0]
        for vtype, pattern in self.RULES:
            for m in pattern.finditer(body):
                line = start_line + body[:m.start()].count("\n")
                issues.append({
                    "file_path": file_path, "line_number": line,
                    "issue_type": "security", "risk_level": "high",
                    "issue_description": f"detected {vtype.replace('_', ' ')}",
                    "suggestion": "fix it", "related_rules": [], "confidence": "high",
                })
        return schema.model_validate({"issues": issues})

    def health(self):
        return True


def make_llm_and_retriever(args):
    settings = Settings()
    if args.llm == "fake":
        return settings, KeywordFakeLLM(), None
    from pr_reviewer.llm import get_backend
    llm = get_backend(settings)
    if args.no_rag:
        return settings, llm, None
    from pr_reviewer.rag import StandardsRetriever
    retriever = StandardsRetriever(settings)
    try:
        retriever.build_index()
    except FileNotFoundError:
        print("!! 规范文档缺失，本次为无 RAG 降级", file=sys.stderr)
        return settings, llm, None
    return settings, llm, retriever


def type_match(issue, gt_type: str) -> bool:
    if issue.issue_type != "security" and gt_type not in CONVENTION_TYPES:
        return False
    desc = issue.issue_description.lower()
    # 类型等价：描述命中类型关键词表，或直接含类型名（code_injection → "code injection"）
    if gt_type in desc or gt_type.replace("_", " ") in desc:
        return True
    return any(k in desc for k in TYPE_KEYWORDS.get(gt_type, []))


def evaluate(llm, retriever, settings: Settings, limit: int | None = None,
             delay: float = 0.0) -> dict:
    """跑一轮评测。token 用量取"进组前读数 → 出组后差值"：backend 的计数是
    实例级累加的，直接用 result.token_usage 会把上一组的用量算进来。

    limit/delay 是给免费档/分钟级限流网关准备的：先用小样本拿到数字，
    而不是把整轮赌在配额上（429 打崩评测就是这么发生的）。
    """
    ground_truth = json.loads(GT_PATH.read_text(encoding="utf-8"))
    if limit:
        ground_truth = ground_truth[:limit]
    total, matched = 0, 0
    tokens_before = getattr(llm, "token_usage", 0)
    duration = 0.0
    errors = 0                 # 限流/超时/解析耗尽的失败块数：>0 说明本轮数据不可信
    cited = issued = 0          # related_rules 非空率：RAG 可追溯性的直接指标
    rows = []
    for entry in ground_truth:
        diff, contents = load_pr_dir(FIXTURES / "vuln_prs" / entry["pr"])
        result = run_review(settings, diff, contents, llm, retriever)
        if delay:
            time.sleep(delay)
        duration += result.duration_seconds
        errors += result.review_errors
        issued += len(result.issues)
        cited += sum(1 for i in result.issues if i.related_rules)
        pr_matched = 0
        for gt in entry["vulnerabilities"]:
            total += 1
            hit = any(i.file_path == gt["file_path"]
                      and abs(i.line_number - gt["line"]) <= 3
                      and type_match(i, gt["type"])
                      for i in result.issues)
            matched += hit
            pr_matched += hit
        rows.append((entry["pr"], len(entry["vulnerabilities"]), pr_matched,
                     len(result.issues), result.duration_seconds))

    fp_total, fp_rows = 0, []
    clean_dirs = [p for p in sorted((FIXTURES / "clean_prs").iterdir()) if p.is_dir()]
    if limit:
        clean_dirs = clean_dirs[:limit]
    for pr_dir in clean_dirs:
        diff, contents = load_pr_dir(pr_dir)
        result = run_review(settings, diff, contents, llm, retriever)
        if delay:
            time.sleep(delay)
        duration += result.duration_seconds
        errors += result.review_errors
        fp_total += len(result.issues)
        fp_rows.append((pr_dir.name, len(result.issues)))

    n_vuln_prs = len(rows)
    n_clean = len(fp_rows)
    return {
        "recall": matched / total if total else 0.0,
        "matched": matched, "total": total,
        "fp_per_pr": fp_total / n_clean if n_clean else 0.0,
        "fp_total": fp_total,
        "rows": rows, "fp_rows": fp_rows,
        "n_vuln_prs": n_vuln_prs, "n_clean": n_clean,
        "tokens": getattr(llm, "token_usage", 0) - tokens_before,
        "duration": round(duration, 1),
        "errors": errors,
        "cite_rate": cited / issued if issued else 0.0,
    }


def render_md(mode: str, rag: bool, r: dict) -> str:
    lines = [
        f"# 评测结果（LLM={mode}，RAG={'on' if rag else 'off'}）",
        "",
        f"- **召回率：{r['recall']:.0%}**（{r['matched']}/{r['total']}，PRD 目标 ≥ 80%）",
        f"- **误报：平均 {r['fp_per_pr']:.1f} 个/干净 PR**（共 {r['fp_total']}，PRD 目标 ≤ 2）",
        f"- 漏洞 PR {r['n_vuln_prs']} 个，干净 PR {r['n_clean']} 个"
        f"，失败块 {r['errors']}，token {r['tokens']:,}",
    ]
    if mode == "fake":
        lines += [
            "",
            "> 口径说明：`--llm fake` 用的是 KeywordFakeLLM——一组正则预言机，只认识 A 组",
            "> 那 9 类教科书式漏洞的已知模式，**天生看不见 B 组私有约定**（gateway/scheduler/",
            "> Money/cachekey 这类只有内部规范才知道的约定）。所以 fake 模式下 B 组召回为 0",
            "> 是设计使然而非缺陷；这一档只用来验证评测链路自洽（匹配、聚合、报表），",
            "> 不代表模型真实能力。B 组的增益见 experiments/convention-ablation.md。",
        ]
    lines += [
        "",
        "| PR | 标注漏洞数 | 命中 | 报告 issue 数 | 耗时(s) |",
        "|---|---|---|---|---|",
        *[f"| {pr} | {n} | {m} | {rep} | {t:.1f} |" for pr, n, m, rep, t in r["rows"]],
        "",
        "| 干净 PR | 误报数 |", "|---|---|",
        *[f"| {pr} | {n} |" for pr, n in r["fp_rows"]],
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--llm", choices=["fake", "real"], default="fake")
    parser.add_argument("--no-rag", action="store_true")
    parser.add_argument("--out", default=None)
    parser.add_argument("--limit", type=int, default=None,
                        help="只跑前 N 个漏洞 PR + N 个干净 PR（网关限流时先出试点数据）")
    parser.add_argument("--delay", type=float, default=0.0,
                        help="每次审查后静默秒数，避开分钟级限流")
    args = parser.parse_args()

    settings, llm, retriever = make_llm_and_retriever(args)
    r = evaluate(llm, retriever, settings, limit=args.limit, delay=args.delay)
    md = render_md(args.llm, retriever is not None, r)
    print(md)
    if args.out:
        Path(args.out).write_text(md, encoding="utf-8")
        print(f"saved -> {args.out}")


if __name__ == "__main__":
    main()
