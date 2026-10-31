# 评测结果（LLM=fake，RAG=off）

- **召回率：59%**（10/17，PRD 目标 ≥ 80%）
- **误报：平均 0.0 个/干净 PR**（共 0，PRD 目标 ≤ 2）
- 漏洞 PR 17 个，干净 PR 10 个，失败块 0，token 7,300

> 口径说明：`--llm fake` 用的是 KeywordFakeLLM——一组正则预言机，只认识 A 组
> 那 9 类教科书式漏洞的已知模式，**天生看不见 B 组私有约定**（gateway/scheduler/
> Money/cachekey 这类只有内部规范才知道的约定）。所以 fake 模式下 B 组召回为 0
> 是设计使然而非缺陷；这一档只用来验证评测链路自洽（匹配、聚合、报表），
> 不代表模型真实能力。B 组的增益见 experiments/convention-ablation.md。

| PR | 标注漏洞数 | 命中 | 报告 issue 数 | 耗时(s) |
|---|---|---|---|---|
| pr-01-hardcoded-secret | 1 | 1 | 1 | 0.0 |
| pr-02-sql-injection | 1 | 1 | 1 | 0.0 |
| pr-03-command-injection | 1 | 1 | 1 | 0.0 |
| pr-04-insecure-deserialize | 1 | 1 | 1 | 0.0 |
| pr-05-path-traversal | 1 | 1 | 1 | 0.0 |
| pr-06-weak-hash | 1 | 1 | 1 | 0.0 |
| pr-07-tls-verify-disabled | 1 | 1 | 1 | 0.0 |
| pr-08-unsafe-yaml | 1 | 1 | 1 | 0.0 |
| pr-09-eval-injection | 1 | 1 | 1 | 0.0 |
| pr-10-xss | 1 | 1 | 1 | 0.0 |
| pr-p01-route-not-gateway | 1 | 0 | 0 | 0.0 |
| pr-p02-db-outside-repository | 1 | 0 | 0 | 0.0 |
| pr-p03-internal-call-plain-requests | 1 | 0 | 0 | 0.0 |
| pr-p04-scheduler-thread | 1 | 0 | 0 | 0.0 |
| pr-p05-money-float | 1 | 0 | 0 | 0.0 |
| pr-p06-raw-exception | 1 | 0 | 0 | 0.0 |
| pr-p07-cachekey-concat | 1 | 0 | 0 | 0.0 |

| 干净 PR | 误报数 |
|---|---|
| pr-c01-refactor | 0 |
| pr-c02-typing | 0 |
| pr-c03-logging | 0 |
| pr-c04-dataclass | 0 |
| pr-c05-contextmanager | 0 |
| pr-c06-retry | 0 |
| pr-c07-listcomp | 0 |
| pr-c08-env-read | 0 |
| pr-c09-json | 0 |
| pr-c10-unittest | 0 |
