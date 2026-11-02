# PR 智能审查与总结工具

[![CI](https://github.com/xiaoqingxu271/smart-pr-reviewer/actions/workflows/ci.yml/badge.svg)](https://github.com/xiaoqingxu271/smart-pr-reviewer/actions/workflows/ci.yml)

输入 PR Diff，输出包含风险点、修改建议、关联规范条款与 PR 摘要的结构化报告。
完整需求见 [PRD.md](PRD.md)，部署与接进 GitHub 见 [DEPLOY.md](DEPLOY.md)。

## 快速开始

```bash
# 1. 安装（Python 3.11+；仓库钉 3.12，uv 按 .python-version 自动装）
uv sync --dev --extra embed   # embed = 本地 embedding（sentence-transformers + CPU torch）
                              # 内网/离线部署设 HF_HUB_OFFLINE=1，模型走 ~/.cache/huggingface

# 2. 配置
cp .env.example .env # 填入 LLM 后端配置；embedding 默认 local（源码不出机器）；隐私审查用 ollama

# 3. 构建规范索引（改规范文档或换 embedding 模型后重跑，索引指纹会自动判定需不需要重建）
python -m scripts.build_standards_index

# 3.5 核对检索是否真的生效（送审块 → 条款命中情况，别等报告出来才发现空转）
python -m scripts.check_retrieval

# 4. 启动服务
uvicorn pr_reviewer.api.main:app --reload

# 5. 调用（/review* 需要 Bearer token；未配 API_TOKEN 时接口一律 503 停用）
curl -X POST localhost:8000/review \
  -H "Authorization: Bearer dev-local-8f3c21a9" -H "Content-Type: application/json" \
  -d '{"mode":"api","repo_url":"https://github.com/you/repo","pr_number":12}'
curl localhost:8000/review/<review_id> -H "Authorization: Bearer dev-local-8f3c21a9"

# 6. 测试（全离线：embedding 用确定性 hash，LLM 用 FakeLLM，不联网不花钱）
pytest

# 7. 静态检查（CI 里跑的就是这两条）
ruff check .
mypy
```

## 目录结构

```
src/pr_reviewer/
├── models.py          # Pydantic 数据模型（CodeChunk / CodeReviewIssue / PRReviewResult）
├── config.py          # Pydantic Settings 配置
├── diff_extractor.py  # 需求1.1：diff 解析 + 行号映射 + 噪音过滤（M1 ✅ 已实现）
├── chunker.py         # 需求1.2：AST/正则/按行三级分块降级链（M2 ✅ 已实现）
├── rag.py             # 需求1.3：条款切块 + embedding 后端（local/ollama/openai/hash）
│                      #   + ChromaDB 索引（带模型指纹）+ top1 门限/margin 检索判据
├── reviewer.py        # 需求1.4：结构化审查 + 幻觉校验 + 去重 + 检索仪表
├── pipeline.py        # 流水线编排（M5）
├── report.py          # Markdown 报告生成
├── llm/               # OpenAI / Ollama 双后端（M4）
└── api/               # FastAPI 异步任务服务（M5）
```

## 接进真实 PR 流程（Webhook）

```bash
# 1. 配好三样东西（.env）：API_TOKEN、WEBHOOK_SECRET、GITHUB_TOKEN
#    GITHUB_TOKEN 要能读仓库 + 写评论：fine-grained 令牌勾 Contents(读) + Pull requests(读写)
#    + Issues(读写)——摘要评论走 issues/{n}/comments 接口；缺任一项对应能力 fail-closed 停用
# 2. 暴露服务（内网穿透或反代），在仓库 Webhooks 里填：
#    URL:    https://<你的服务>/webhook/github
#    Content type: application/json
#    Secret: 与 WEBHOOK_SECRET 一致
#    Events: Pull request
# 3. 之后每次 PR opened / synchronize / reopened，问题会作为行内评论出现在代码行下面，
#    并附一条摘要评论（含命中的规范条款号与原文摘录）
curl -H "Authorization: Bearer $API_TOKEN" localhost:8000/health   # 看 publish_enabled
```

只配 webhook 没配 `GITHUB_TOKEN` 时事件会被**明确忽略**（返回 `ignored`）而不是白烧
LLM 配额——审完没人看的结果不该花钱。

## 里程碑进度

- [x] M1 Diff 提取 + 行号映射 + 噪音过滤（测试覆盖）
- [x] M2 AST 分块 + 降级链（5000 行实验待跑）
- [x] M3 RAG 规范库 + 检索（本地 bge embedding + top1 门限/margin 判据；选型与阈值标定见
      [experiments/rag-embedding-bench.md](experiments/rag-embedding-bench.md)，
      真实链路命中率用 `python -m scripts.check_retrieval` 复核）
- [x] M4 LLM 审查 + 重试 + 幻觉校验（Fake LLM 离线覆盖；真实后端联调待做）
- [x] M5 FastAPI 服务 + 报告 + 缓存（TestClient 全链路测试覆盖）
- [x] M6 评测脚本 + 三组破坏性实验（实验 1 已出数据；实验 2/3 与真实评测待接 LLM）
