"""把 CWE 官方目录切成三档**嵌套**语料，用于"检索层随规模怎么退化"的测量。

数据来源全部是第三方的，脚本不产生任何规则文本：
- 规则文本：MITRE CWE 官方 XML（data/raw/cwec_latest.xml.zip，版本与 sha256 记进 manifest）
- 哪些条款必须进每一档：Bandit 在你真实代码上跑出的 CWE 编号（data/labels/…）

"嵌套"是关键设计：小档是大档的子集，且**期望条款在每一档都存在**。否则 100 档里
恰好没标注答案的查询会被算成漏召，曲线就不只是"干扰项变多"的影响，数字不可比。

用法：
    uv run python scripts/build_cwe_corpus.py
产物：data/corpus/cwe-tier-{13|100|955}.md + data/corpus/manifest.json
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

NS = "http://cwe.mitre.org/cwe-8"     # CWE 5.0 的命名空间
NEWLINE = chr(10)                     # 避免源码里到处写转义换行（正则与拼接都更易读）
REPO = Path(__file__).resolve().parent.parent
RAW_ZIP = REPO / "data" / "raw" / "cwec_latest.xml.zip"
LABELS = REPO / "data" / "labels" / "bandit_findings.json"
CORPUS = REPO / "data" / "corpus"

SOURCE_URL = "https://cwe.mitre.org/data/xml/cwec_latest.xml.zip"
SEED = 20261102          # 固定抽样种子：同一台机器、同一份 XML 必须得到同一份语料
TIERS = (100,)           # 期望条款档 + 这几档 + 全量档
# Bandit 的 B101 是"代码里用了 assert"，它自己的文档就建议在测试目录里忽略这条；
# 留着会让 859 条查询里 196 条其实是同一种噪声。显式剔除并记下数量，不静默丢。
DROP_TESTS = {"B101"}


def strip_xhtml(fragment: str) -> str:
    """CWE 的描述里混了 xhtml 标签，取纯文本。"""
    text = re.sub(r"<[^>]+>", " ", fragment)
    return re.sub(r"\s+", " ", text.replace("&quot;", '"').replace("&amp;", "&")
                  .replace("&lt;", "<").replace("&gt;", ">")).strip()


def extract_example_code(node) -> str:
    """CWE 官方 Demonstrative_Examples 里的代码段（Example_Code，Nature=Bad/Attack）。

    取它有两个理由：一是它让"条款带不带代码示例"这个变量可以被单独测量——
    我们自己的 17 条规范补了典型违规示例后合成用例从 0/8 跳到 10/10，
    但那是合成集；二是它是第三方写的代码，不是我编的示例。
    """
    out = []
    for ex in node.iter(f"{{{NS}}}Example_Code"):
        raw = ET.tostring(ex, encoding="unicode")
        raw = re.sub(r"<[^>]*br\s*/?>", NEWLINE, raw)
        raw = re.sub(r"<[^>]+>", " ", raw)
        raw = re.sub(NEWLINE + r"[ \t]+", NEWLINE, raw)
        code = raw.strip()
        if len(code) > 20:
            out.append(code[:600])
    return NEWLINE.join(out)


def load_weaknesses() -> tuple[list[dict], dict]:
    xml_name = zipfile.ZipFile(RAW_ZIP).namelist()[0]
    raw = zipfile.ZipFile(RAW_ZIP).read(xml_name)
    root = ET.fromstring(raw)
    ns = re.match(r"\{.*\}", root.tag).group(0).strip("{}")
    catalog = root.attrib
    out = []
    for w in root.iter(f"{{{ns}}}Weakness"):
        if w.get("Status") == "Deprecated":          # 官方已废弃的条目不该进规范库
            continue
        desc = w.find(f"{{{ns}}}Description")
        text = strip_xhtml(desc.text or "") if desc is not None else ""
        if not text:
            continue
        out.append({"id": int(w.get("ID")), "name": w.get("Name"), "text": text,
                    "abstraction": w.get("Abstraction"), "status": w.get("Status"),
                    "code": extract_example_code(w)})
    meta = {"xml_file": xml_name, "catalog_name": catalog.get("Name"),
            "catalog_version": catalog.get("Version"), "catalog_date": catalog.get("Date"),
            "zip_sha256": hashlib.sha256(RAW_ZIP.read_bytes()).hexdigest()}
    return out, meta


def expected_ids() -> tuple[dict[int, int], dict]:
    """返回 {CWE 编号: 查询条数} 与标注元信息。"""
    data = json.loads(LABELS.read_text(encoding="utf-8"))
    findings = [f for f in data["findings"] if f.get("test_id") not in DROP_TESTS]
    counts: dict[int, int] = {}
    for f in findings:
        if f.get("cwe_id") is not None:
            counts[f["cwe_id"]] = counts.get(f["cwe_id"], 0) + 1
    return counts, {"findings_total": len(data["findings"]), "findings_used": len(findings),
                    "findings_dropped_by_test_id": len(data["findings"]) - len(findings),
                    "dropped_test_ids": sorted(DROP_TESTS),
                    "files_scanned": data["files_scanned"],
                    "bandit_version": data["bandit_version"],
                    "roots": data["roots"]}


def write_rules(path: Path, rules: list[dict], with_code: bool = False) -> None:
    """切成项目解析器认的格式：`## <编号> <标题>` + 空行 + 条款正文。

    with_code=True 时追加官方代码示例——这是"同一批条款、只改写法"的对照组。
    """
    lines = [f"# CWE 软件弱点目录（{'带官方代码示例' if with_code else '仅官方描述'}）", ""]
    for r in rules:
        lines += [f"## CWE-{r['id']:04d} {r['name']}", "", r["text"], ""]
        if with_code and r["code"]:
            lines += ["典型违规示例：", "", "```", r["code"], "```", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    if not RAW_ZIP.exists() or not LABELS.exists():
        print("先跑 scripts/label_with_bandit.py，并下载 CWE 官方 XML 到 data/raw/",
              file=sys.stderr)
        return 2

    weaknesses, meta = load_weaknesses()
    by_id = {w["id"]: w for w in weaknesses}
    counts, label_meta = expected_ids()
    # 标注里的编号可能已被官方废弃（如 CWE-377 → Bandit 仍标它）。废弃条目不进规范库，
    # 所以这些查询本质上不可召回：显式剔除并计数，而不是留在分母里冒充漏召。
    unrecallable = {cwe: n for cwe, n in counts.items() if cwe not in by_id}
    need = {cwe for cwe in counts if cwe in by_id}
    if unrecallable:
        print(f"  注意：{sum(unrecallable.values())} 条查询的期望条款已被 CWE 官方废弃，"
              f"从分母剔除：{unrecallable}")

    CORPUS.mkdir(parents=True, exist_ok=True)
    rng = random.Random(SEED)

    required = [by_id[i] for i in sorted(need)]
    rest = [w for w in weaknesses if w["id"] not in need]
    tiers: dict[str, dict] = {}

    def emit(name: str, rules: list[dict], with_code: bool = False) -> None:
        path = CORPUS / f"cwe-tier-{name}.md"
        write_rules(path, rules, with_code)
        tiers[name] = {"rule_count": len(rules), "file": path.name, "with_code": with_code,
                       "rules_with_official_code": sum(1 for r in rules if r["code"]),
                       "rule_ids": [r["id"] for r in rules]}
        print(f"  {name:>5} 条（带官方示例 {sum(1 for r in rules if r['code'])}）"
              f"{' +代码' if with_code else ''} → {path.name}")

    emit(f"{len(required)}", required)                      # 只有期望条款：无干扰下限
    for size in TIERS:
        if size <= len(required):
            continue
        filler = rng.sample(rest, size - len(required))
        emit(str(size), sorted(required + filler, key=lambda w: w["id"]))
    emit(str(len(weaknesses)), weaknesses)                   # 全量：真实规范库规模上限
    emit(f"{len(weaknesses)}+code", weaknesses, with_code=True)   # 同一批条款，只改写法

    manifest = {"source_url": SOURCE_URL, **meta, "seed": SEED,
                "deprecated_excluded": True, "labels": label_meta,
                "unrecallable_cwes": unrecallable,
                "expected_cwes": sorted(need), "tiers": tiers}
    (CORPUS / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1),
                                          encoding="utf-8")
    print(f"\nCWE {meta['catalog_version']}（{meta['catalog_date']}）"
          f" 非废弃条目 {len(weaknesses)} 条；标注覆盖 {len(need)} 个 CWE 编号")
    print(f"manifest → {CORPUS / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
