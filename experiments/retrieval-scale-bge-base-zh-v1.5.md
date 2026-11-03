# 检索层随语料规模的退化（真实 CWE 语料 + Bandit 真实标注）

- 规则来源：https://cwe.mitre.org/data/xml/cwec_latest.xml.zip（CWE 5.0，2026-10-28，zip sha256 `bcda44c9abf61ff9`）
- 标注来源：Bandit 1.9.4 扫描 1012 个真实源码文件（4 个目录），取 663 条 finding 的 CWE 编号
- 剔除：`['B101']`（Bandit 自己就建议在测试码里忽略）共 196 条；期望条款已被官方废弃而不可召回 18 条
- 查询：167 个语法块（12 个 CWE，每种上限 20 条）；干净对照 120 块；种子 20261102
- 参数：embedder=st:BAAI/bge-base-zh-v1.5，门限=0.45，top_k=5，margin=0.05
- 结果哈希：`c96afef1f8d240d7`（同机重跑应完全一致）

| 语料条数 | 带官方示例 | recall@1 | recall@3 | recall@5 | recall@10 | recall@20 | 随机@1 | 随机@5 | 随机@20 | MRR | 门限0.45保留 | 干净块误放行 | 干扰项top1 median/p90 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 12 | 12 | 0.08 | 0.31 | 0.47 | 0.87 | 1.00 | 0.08 | 0.42 | 1.00 | 0.27 | 0.43 | 0.82 | 0.543 / 0.599 |
| 100 | 58 | 0.02 | 0.04 | 0.05 | 0.10 | 0.23 | 0.01 | 0.05 | 0.20 | 0.04 | 0.05 | 0.94 | 0.586 / 0.650 |
| 956 | 549 | 0.01 | 0.02 | 0.02 | 0.04 | 0.04 | 0.00 | 0.01 | 0.02 | 0.02 | 0.02 | 0.97 | 0.615 / 0.662 |
| 956 | 549 | 0.02 | 0.07 | 0.09 | 0.12 | 0.17 | 0.00 | 0.01 | 0.02 | 0.05 | 0.09 | 0.99 | 0.679 / 0.748 |

重建（`data/` 不入库：CWE 原始包 2 MB、标注里带本机绝对路径）：

```bash
curl -L -o data/raw/cwec_latest.xml.zip https://cwe.mitre.org/data/xml/cwec_latest.xml.zip
uv run --extra eval python scripts/label_with_bandit.py --roots <你的真实代码目录>
uv run --extra eval python scripts/build_cwe_corpus.py
HF_HUB_OFFLINE=1 uv run --extra embed --extra eval python scripts/bench_retrieval_scale.py
```

读法：`recall@k` 是**关掉门限**的纯排序质量；`门限0.45保留` 是生产口径下正确条款仍出现在返回结果里的比例；`干净块误放行` 是没有 finding 的代码块被塞进条款的比例。逐查询明细见 `data/corpus/scale-results.json`。
