# 破坏性实验记录（PRD §7.3）

| 实验 | 状态 | 记录文件 / 结果 |
|---|---|---|
| 1. 5000 行巨型文件：按行切 vs AST 切 | ✅ 已完成（离线） | [exp1-giant-file.md](exp1-giant-file.md)：函数切断率 99% → **0%** |
| 2. 无 RAG vs 有 RAG 召回率对照 | ⏳ 脚本就绪，待真实 LLM | `python scripts/exp2_rag_ablation.py` → exp2-rag-ablation.md |
| 3. Prompt 注入有/无防护对比 | ⏳ 脚本就绪，待真实 LLM | `python scripts/exp3_prompt_injection.py` → exp3-prompt-injection.md |

评测主结果（fake 理想审查员，验证评测链路自洽）：[eval-results.md](eval-results.md)。
接入真实 LLM 后运行 `python scripts/run_eval.py --llm real --out experiments/eval-results.md`
覆盖为真实数字（fake 模式召回 100% / 误报 0 仅代表框架正确性，不代表模型水平）。

模板：实验目的 → 设置 → 数据 → 结果表格 → 一句话结论（面试口径）。
