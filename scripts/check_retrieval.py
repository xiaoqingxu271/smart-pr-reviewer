"""规范检索链路核对（PRD §5.3）：用真实配置跑一遍，确认条款确实被召回到块上。

与 bench_rag_embeddings.py 的区别：那边选型（模型/判据/阈值），这边验现状——
它走的是生产的 `StandardsRetriever`（读 .env、用 .chroma 索引、用真实判据参数），
所以换模型或改阈值后跑一次就知道链路是否真的通。

    python -m scripts.check_retrieval          # 需要 embedding 依赖已装
输出三项：条款可追溯率（期望块命中）、漏召性质（空结果 vs 错置）、干净块放行率。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from pr_reviewer.config import get_settings
from pr_reviewer.pipeline import _select_chunks
from pr_reviewer.rag import INDEX_META_FILE, StandardsRetriever
from run_eval import FIXTURES, GT_PATH, TYPE_TO_RULE, load_pr_dir

# 漏洞类型 → 条款号由 run_eval.TYPE_TO_RULE 单点维护，避免三处漂移

settings = get_settings()
r = StandardsRetriever(settings)
meta = json.loads((settings.chroma_dir / INDEX_META_FILE).read_text(encoding="utf-8"))
print(f"配置 backend={settings.embedding_backend} model={settings.embedding_model}")
print(f"索引 embedder={meta['embedder']} dim={meta['dim']} 条款={meta['rule_count']}")
print(f"判据 top1门限={settings.retrieval_score_threshold} margin={settings.retrieval_margin} "
      f"top_k={settings.retrieval_top_k}\n")

kept = missed_empty = total = 0
for entry in json.loads(GT_PATH.read_text(encoding="utf-8")):
    rule = TYPE_TO_RULE.get(entry["vulnerabilities"][0]["type"])
    if not rule:
        continue
    total += 1
    v = entry["vulnerabilities"][0]
    diff, contents = load_pr_dir(FIXTURES / "vuln_prs" / entry["pr"])
    chunks, _sk, _dg = _select_chunks(settings, diff, contents)
    same_file = [c for c in chunks if c.file_path == v["file_path"]]
    # 生产链路会对每个送审块各检索一次；漏洞所在块是关键块
    covering = [c for c in same_file if c.start_line <= v["line"] <= c.end_line]
    target = min(covering, key=lambda c: c.end_line - c.start_line) if covering else None
    hits = r.retrieve(target.content) if target else []
    ids = [h.rule_id for h in hits]
    ok = rule in ids
    kept += ok
    missed_empty += (not ok and not hits)
    all_hits = {c.start_line: [h.rule_id for h in r.retrieve(c.content)] for c in same_file}
    print(f"{entry['pr']:26s} 期望{rule} 关键块L{target.start_line}-{target.end_line} → "
          f"{ids or '（空）'} {[h.score for h in hits]} {'✓' if ok else '✗'}"
          f"　全块={all_hits}")

print(f"\n规范条款可追溯率 = {kept}/{total}；漏召且为空（非错置）= {missed_empty}")

# 干净块：看有多少会被放行
clean_leak = clean_total = injected = blocks = 0
for pr_dir in sorted((FIXTURES / "clean_prs").iterdir()):
    if not pr_dir.is_dir():
        continue
    diff, contents = load_pr_dir(pr_dir)
    chunks, _sk, _dg = _select_chunks(settings, diff, contents)
    for c in chunks:
        if not c.changed_lines:
            continue
        clean_total += 1
        hits = r.retrieve(c.content)
        blocks += 1
        injected += len(hits)
        clean_leak += bool(hits)
print(f"干净块放行 = {clean_leak}/{clean_total}"
      f"　平均注入条款数 = {injected / blocks:.1f} 条/块（抬 top_k 的代价在这里）")
