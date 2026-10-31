"""评测匹配判据的测试（scripts/run_eval.py 的命中口径）。

判据本身是评测系统里最容易悄悄偏掉的一环：太松会把无 RAG 也判成命中（曾发生，
关键词用了 route/连接 这类通用词，模型引用代码行就中招），太严会把真检出判成漏检
（模型复述"用 float 累加金额"却没写专名 Money）。所以两种口径都要有测试钉住。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from pr_reviewer.models import CodeReviewIssue
from run_eval import TYPE_KEYWORDS, TYPE_TO_RULE, gt_match

GT = {"file_path": "app/billing/invoice.py", "line": 4, "type": "money_as_float"}


def issue(line=4, type_="security", desc="使用 float 累加金额，存在精度误差", rules=()):
    return CodeReviewIssue(file_path=GT["file_path"], line_number=line, issue_type=type_,
                           risk_level="medium", issue_description=desc,
                           suggestion="改用 Money 类型", related_rules=list(rules))


def test_cited_rule_rescues_keyword_false_negative():
    """描述没写专名，但引用了对应条款 → 严格口径漏、引用口径命中。"""
    strict, cited = gt_match([issue(rules=["SEC-015"])], GT)
    assert strict is False and cited is True


def test_keyword_path_still_counts():
    strict, cited = gt_match([issue(desc="money 类型才能避免浮点误差")], GT)
    assert strict is True and cited is False


def test_wrong_position_never_matches_even_with_rule_cited():
    """位置不对就不算命中——引用条款不能替代"指对了行"。"""
    assert gt_match([issue(line=99, rules=["SEC-015"])], GT) == (False, False)
    wrong_file = issue(rules=["SEC-015"]).model_copy(update={"file_path": "else/where.py"})
    assert gt_match([wrong_file], GT) == (False, False)


def test_line_within_three_lines_still_matches():
    assert gt_match([issue(line=6, rules=["SEC-015"])], GT)[1] is True


def test_every_annotated_type_has_rule_and_keywords():
    """标注里出现过的类型必须同时有条款号和关键词表。

    缺任何一条都会让对应工具静默少测：没有条款号就没有引用口径，没有关键词就只能靠
    类型名巧合命中——这类"漏配"不会报错，只会让数字看着正常地错掉。
    """
    gt_path = Path(__file__).resolve().parent / "ground_truth.json"
    types = {v["type"] for entry in json.loads(gt_path.read_text(encoding="utf-8"))
             for v in entry["vulnerabilities"]}
    assert types, "标注文件读不到任何漏洞类型"
    missing_rule = sorted(t for t in types if not TYPE_TO_RULE.get(t))
    missing_keyword = sorted(t for t in types if not TYPE_KEYWORDS.get(t))
    assert not missing_rule, f"以下类型没有条款映射（覆盖率缺口）：{missing_rule}"
    assert not missing_keyword, f"以下类型没有关键词：{missing_keyword}"
