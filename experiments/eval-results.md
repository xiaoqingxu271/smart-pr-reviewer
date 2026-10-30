# 评测结果（LLM=fake，RAG=off）

- **召回率：100%**（10/10，PRD 目标 ≥ 80%）
- **误报：平均 0.0 个/干净 PR**（共 0，PRD 目标 ≤ 2）
- 漏洞 PR 10 个，干净 PR 10 个

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
