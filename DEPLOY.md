# 部署手册（DEPLOY.md）

面向"把它当成团队内部服务挂起来跑"的操作手册。命令以 Linux/macOS 为主，Windows 差异在
括号里标注。凡"留空即停用"的配置都是 **fail-closed** 设计，不要为了跑通便填个值了事。

配套规格见 [PRD.md](PRD.md)，检索参数依据见 [experiments/rag-embedding-bench.md](experiments/rag-embedding-bench.md)。

---

## 0. 前置条件

| 依赖 | 要求 | 说明 |
|---|---|---|
| Python | 3.11+（仓库钉 3.12） | `requires-python` 是下限；`.python-version` 让 CI 与本地跑同一个解释器 |
| uv | 任意近期版本 | 也可 `pip install -e ".[dev]"`；CI 用 `uv sync --frozen`，依赖锁 `uv.lock` |
| git | 2.30+ | API 模式取文件内容要用 `git fetch`（浅取克隆） |
| LLM 后端 | OpenAI 兼容网关 或 Ollama | 见 §3 |
| GitHub | 一个 PAT + 仓库 webhook 权限 | 只用 HTTP API 手动提交审查时可跳过 |

不需要 Docker，不需要 GPU。embedding 在 CPU 上跑，7~17 条款的规范库索引耗时以秒计。

---

## 1. 三种部署形态，先选一种

| 形态 | 用途 | 需要公网地址 | 需要 webhook |
|---|---|---|---|
| A 本机试用 | 手动 POST 审某个 PR，看报告 | 否 | 否 |
| B 内网常驻服务 | 团队内 http://内网IP:8000 调用，CI 里手动触发 | 否 | 否 |
| C 自动评审 | PR 一开/一推就自动出行内评论 | **是**（GitHub 要回调到你） | 是 |

形态 C 才需要 §6 的隧道/反代。先用 A 跑通再上 C，别一上来就配 webhook——排障时你会分不清
是签名问题、token 权限问题还是检索问题。

---

## 2. 安装与首次启动

```bash
git clone https://github.com/xiaoqingxu271/smart-pr-reviewer.git
cd smart-pr-reviewer

# 本地 embedding（源码不出机器）需要 torch CPU 版；只用 Ollama/网关 embedding 时可去掉 --extra embed
uv sync --dev --extra embed

cp .env.example .env
```

`.env` 至少填这几项才能起来干活（其余有默认值）：

```ini
LLM_BACKEND=openai
MODEL_NAME=<你的模型名>
OPENAI_API_KEY=<网关 key>
OPENAI_BASE_URL=https://<网关>/v1        # 填到 /v1，不要带 /chat/completions

API_TOKEN=<随机值>                        # 留空 = /review 一律 503
```

生成随机值：`python -c "import secrets;print(secrets.token_hex(32))"`。

### 关于 embedding 模型的一次性下载

默认模型 `BAAI/bge-base-zh-v1.5`（768 维）首次加载会从 Hugging Face 拉约 0.4 GB 到
`~/.cache/huggingface/hub`。**内网/离线部署**请先在有网机器上下好，或设：

```bash
export HF_HUB_OFFLINE=1        # 只走本地缓存，不去探测 Hub（否则启动时先卡一次网络超时）
```

模型不在缓存里又设了 offline，代码会按"处处可降级"退回 hash 词袋并**打一条 warning**——
此时检索质量与阈值都不对（hash 的分数口径完全不同），必须显式改
`RETRIEVAL_SCORE_THRESHOLD`（实测 top1 均分只有 0.34 上下），别拿 0.45 用。

---

## 3. 索引构建与检索自检（**别跳过这步**）

```bash
python -m scripts.build_standards_index     # 解析规范 md → 建 Chroma 索引
python -m scripts.check_retrieval          # 用真实配置跑一遍，看条款到底召不召得回
```

`check_retrieval` 的输出应当是这样（当前规范库 17 条款、17 个漏洞块 + 15 个干净块）：

```
规范条款可追溯率 = 17/17；漏召且为空（非错置）= 0
干净块放行 = 10/15　平均注入条款数 = 1.8 条/块
```

**这一步为什么是硬要求**：本项目最初的状态是"检索一直在空转，而报告和'代码很干净'长得
一模一样"。索引没建、模型不对、阈值错配，都不会报错，只会让报告里再没有出现"命中规范"。

改了 `docs/研发代码安全规范.md` 之后要重跑 `build_standards_index`：索引指纹
（`.chroma/.standards.index.json` 记录规范文件 hash + embedding 模型名 + 维度）会自动判定
是否需要重建；换 embedding 模型同样会触发重建（不同向量空间不允许混库）。

改了规范文档或换模型后，**阈值必须重新标定**：

```bash
python scripts/bench_rag_embeddings.py      # 输出正确条款分数带 vs 干净块分数带
```

当前 0.45 与最弱正确条款（0.460）只差 0.01，没有余量。

---

## 4. 启动服务

```bash
uvicorn pr_reviewer.api.main:app --host 0.0.0.0 --port 8000
```

进程模型要注意（这是本项目当前设计里最需要知情的一点）：

- 任务态与结果缓存在 SQLite（`DB_PATH`，WAL 模式），所以**多 worker 之间状态是共享的**，
  重启后 `review_id` 仍可查；
- 但并发与排队上限（`MAX_CONCURRENT_REVIEWS` / `MAX_PENDING_REVIEWS`）是**每进程**的。
  `--workers 4` 实际最多 8 个 PR 同时在审、32 个在排。想精确控总量就只开 1 个 worker，
  靠 `MAX_CONCURRENT_REVIEWS` 调；
- 生产请放在反向代理后面（Caddy/Nginx 做 TLS）。服务自身不处理 HTTPS。

自检：

```bash
curl -s localhost:8000/health | python -m json.tool
```

期望看到（各字段含义见 §5）：

```json
{"status":"ok","llm_backend":"openai","llm":true,
 "rag":{"available":true,"index_size":17},
 "auth_configured":true,"local_mode_allowed":false,
 "webhook_configured":false,"publish_enabled":false,
 "load":{"pending":0,"max_pending":20,"workers":2,"rejected_total":0},"db":true}
```

`rag.available=false` 或 `index_size=0` = 索引没建或空转，先别接流量。

---

## 5. 配置项速查（只列会咬人的）

| 变量 | 默认 | 留空/误配的后果 |
|---|---|---|
| `API_TOKEN` | 空 | **`/review*` 全部 503 停用**（fail-closed，故意的） |
| `WEBHOOK_SECRET` | 空 | `/webhook/github` 503 停用；配了才能接 GitHub 回调 |
| `GITHUB_TOKEN` | 空 | 不写回 PR；且**webhook 事件会被明确忽略**（不白烧 LLM 配额） |
| `ALLOWED_REPO_ROOTS` | 空 | `mode=local` 禁用。要审本机仓库才填，且只填必要目录 |
| `EMBEDDING_BACKEND` | `local` | 改 `ollama` 时注意 `bge-m3` **不需要**检索指令，要把 `EMBEDDING_QUERY_PREFIX` 置空并重标阈值 |
| `RETRIEVAL_SCORE_THRESHOLD` | `0.45` | 判 top1。抬太高会整块拒答（可追溯率归零），调低会塞无关条款 |
| `RETRIEVAL_MARGIN` | `0.05` | 裁尾，决定除冠军外还附带几条。生产实测平均 1.8 条/块 |
| `RETRIEVAL_TOP_K` | `5` | 截断位。**语料变大时必须复核**（12 条款时正确条款掉到第 5 名就差点被切掉） |
| `REVIEW_DEADLINE_SECONDS` | `600` | 超时返回部分结论并在报告里标 ⏳，不静默假装审完 |
| `LLM_MAX_RETRIES` | `3` | 网关 429/超时的退避重试次数；评测跑不动时先加大它 |
| `TOKEN_BUDGET` | `100000` | 单批送审的 token 预算，分批用 |
| `CACHE_TTL_HOURS` | `24` | 结果缓存时效，key 含 head sha，同 sha 不重复烧钱 |

---

## 6. 接进 GitHub（形态 C）

### 6.1 令牌

Fine-grained PAT 或 classic token，权限：

- `Contents: Read`（取 PR 代码内容，若走浅取克隆）/ 或 `metadata:read` + API contents；
- **`Pull requests: Write`** —— 没有这个就发不出评论，是最常见的踩坑点。

若用 GitHub App，注意 installation token 有效期 1 小时，当前实现**不做自动续期**，
长期驻留要配 PAT 或自行加刷新逻辑。

### 6.2 让它能被回调

本机最快的方式（临时验证用）：

```bash
cloudflared tunnel --url http://localhost:8000     # 给出 https://xxx.trycloudflare.com
# 或： ngrok http 8000
```

正式部署用固定域名的反向代理 + HTTPS。

### 6.3 仓库侧配置

目标仓库 → Settings → Webhooks → Add webhook：

| 项 | 值 |
|---|---|
| Payload URL | `https://<你的地址>/webhook/github` |
| Content type | `application/json`（**不能是 form-urlencoded**，签名基线不同） |
| Secret | 与 `WEBHOOK_SECRET` 完全一致 |
| Events | *Pull request* |

保存后 GitHub 会发一个 `ping` 事件。日志里应当看到它被**明确忽略**（返回
`{"ignored": true}`）——只处理 `pull_request` 的 `opened / synchronize / reopened`。

### 6.4 第一次真实验证

在一个测试仓库开 PR，故意引入一处私有约定违规（照 `tests/fixtures/vuln_prs/pr-p01-*` 的写法
就行）。应当依次看到：

```
stage=task_start … stage=chunk files=1 chunks=2 … stage=done … rag=True hit_rate=1.00 …
stage=task_done … stage=published task=… inline=1 deferred=0 summary=True
```

并且 PR 的对应代码行下面出现行内评论（含条款号与原文摘录），会话里多一条摘要评论。

---

## 7. 成本与限流（实测值，不是估算）

| 数据集 | 无 RAG | 有 RAG | 增幅 |
|---|---|---|---|
| A 组 20 个 PR（通用漏洞+干净） | 32,848 token | 37,546 token | **+14%** |
| B 组 7 个 PR（私有约定） | 14,024 token | 16,185 token | **+15%** |

即：**每份 PR 约 1.6k~2.3k token，注入规范条款多花 15% 左右**，墙钟时间没有明显增加
（A 组 10 个漏洞 PR + 10 个干净 PR 全跑一遍约 200~230 秒）。

限流是这里最现实的敌人。免费档网关是**分钟级配额**，实测会把评测污染：某次 A/B 两组分别
有 8 和 9 个块审查失败，那份表里的"召回率"毫无意义。所以现在：

- 评测/扫描脚本在**有失败块时直接非零退出**并在报表顶部打 ⚠️，不要采信被污染的表格；
- 批量跑请加大 `LLM_MAX_RETRIES` 与调用间隔（我们用 `--delay 30` 拿到过 0 失败块的干净结果）；
- 生产侧一次 429 只会让**单个块**计为 `review_error`，不会作废整份审查（这点是踩过之后修的）。

---

## 8. 数据与隐私

- **源码出站**：`LLM_BACKEND=openai` 意味着被审代码会送到所配置的网关。要完全不出内网，
  用 `LLM_BACKEND=ollama` + 本地模型；embedding 默认已是本地（`EMBEDDING_BACKEND=local`）。
- **落盘内容**：`reports/*.md`（含源码片段与已脱敏的密钥）、`.data/reviews.sqlite3`
  （任务态 + 结果全文）。这两处**必须设保留策略**——目前实现没有自动清理，属于已知缺口。
- **脱敏**：发生在结果构造层，Markdown 报告和 `GET /review/{id}` 的 JSON 两个出口都覆盖
  （`sk-abcd****`）。规则见 `src/pr_reviewer/masking.py`。
- **日志**：只记计数、分数、耗时、错误类型；不记源码正文、不记任何 token。
- **权限边界**：`ALLOWED_REPO_ROOTS` 不设就禁用 local 模式；行内评论只发 PR 覆盖的行。

---

## 9. 故障排查

| 症状 | 大概率原因 | 处置 |
|---|---|---|
| `/review` 返回 503 | 没配 `API_TOKEN` | 配上并重启（这是设计行为，不是 bug） |
| `/webhook/github` 403 | 签名不匹配：`WEBHOOK_SECRET` 与仓库侧不一致 | 核对两边密钥一致 |
| `/webhook/github` 400 并提示 Content type | 仓库 webhook 的 Content type 不是 `application/json`（签名是对的，别去查密钥） | 改成 `application/json` |
| webhook 返回 `{"ignored":true,"reason":"未配置 GITHUB_TOKEN…"}` | 没配写回令牌 | 加 `Pull requests: Write` 权限的 token |
| 报告里没有"命中规范"、`check_retrieval` 召回低 | 索引没建 / embedding 与阈值不匹配 / 降级成了 hash 词袋 | 依次跑 §3 两条命令，看 `/health` 的 `rag.index_size` |
| 任务一直 `running` | 大 PR 排队中（并发上限默认 2） | 看 `/health` 的 `load.pending`，调 `MAX_CONCURRENT_REVIEWS` |
| 任务 `failed` 且 error 是友好文案 | 细节在服务端日志的 `stage=task_failed` | 按 `review_id` 去日志里找 `error_type` |
| 没有行内评论只有摘要评论 | 行号不在 diff 覆盖范围（合并后漂移，GitHub 返回 422），条目已退化进摘要 | 正常降级；若整份都这样，检查 `commit_id` 是否取的是 PR head |
| 任务 completed 但 PR 上没评论 | 看 `GET /review/{id}` 的 `publish` 字段：`failed` 是 GitHub 侧报错（附异常摘要），`skipped` 是没配 token 或缓存命中没重复发 | 按 `publish.reason` 处置；写回本身在 `completed` 之前完成，所以不会出现"评论还在路上"的中间态 |
| 报告出了但状态还停在 `running` | 写回评论要等 GitHub API（单请求最多 30s，问题多时逐条发） | 属设计取舍：宁可让状态可见得慢，也不让"已完成"撒谎。要立刻拿报告就设 `post_comments=false` 走 `/review` |
| 同一 PR 被反复评审 | `synchronize` 每次 push 都会触发 | 属预期；要抑制就在 CI 侧控制推送频率或加去重（当前未实现） |
| 评测数字看着离谱 | 有失败块（限流） | 报表顶部有 ⚠️ 就别用；加大 `--delay` 与 `LLM_MAX_RETRIES` 重跑 |
| 服务启动卡在联网 | embedding 在探测 HF Hub | `export HF_HUB_OFFLINE=1`（模型须已在缓存里） |

---

## 10. 回滚

按影响面从小到大，逐级即可，**不需要重装**：

```bash
# 1) 只停"写回 PR"（审查照跑，评论不发）
#    .env 里清空 GITHUB_TOKEN，重启服务

# 2) 停 webhook 触发（保留手动 API 调用）
#    .env 里清空 WEBHOOK_SECRET → /webhook/github 直接 503
#    或直接在 GitHub 仓库 Settings → Webhooks 取消 active

# 3) 停整个服务
pkill -f "uvicorn pr_reviewer"        # Windows: 结束对应 python 进程

# 4) 回到某个已知好的版本
git log --oneline -10
git checkout <好的提交>
uv sync --dev --extra embed
python -m scripts.build_standards_index     # 若规范文档也变了才需要
python -m scripts.check_retrieval           # 上线前自检，17/17 才算好
```

任务状态与缓存是可以安全删的（纯派生数据）：

```bash
rm -f .data/reviews.sqlite3*     # 记得一起删 -wal / -shm
rm -rf .chroma                   # 下次 build_standards_index 会重建
```

删除前不必停服务，但删完正在跑的任务会丢状态（进程仍跑，查询 404），所以建议先停服务。

---

## 11. 上线前还缺什么（别当成已具备）

诚实清单，按我建议的处理顺序：

1. **真实 webhook 端到端尚未验证过**——目前只有 `httpx.MockTransport` 的离线测试。第一次
   真回调要盯：422 退化路径、多 commit PR 的 `commit_id` 取值、`synchronize` 频率。
2. **CI 刚接入**，徽章还没绿过一次；先看 Actions 页确认 `ruff/mypy/pytest` 三项在干净环境
   下通过。
3. **`reports/` 与 `.data/` 无保留策略**（会无限增长，且含源码片段）。
4. **排队/并发上限是每进程的**，多 worker 下总量要自己乘。
5. **GitLab 只有取数支持，没有 webhook 与写回**（`/webhook/github` 与 `GitHubPublisher` 是
   GitHub 专属）。要接 GitLab 得补一个端点 + publisher。
6. **没有按调用方的限流与配额**（只有全局队列上限），团队规模变大时需要 per-repo 预算。
7. **rerank 是"扩容触发线"而不是已启用**：17 条款下有 1 例正确条款排在第 5 名、贴着
   `top_k=5` 边界；再加条款就该上 cross-encoder（实测它能把 R@3 从 0.94 抬到 1.00）。
8. 规范库当前是**全局单租户**。真要给多个团队用，需要 per-repo 规范与权限过滤
   （collection 隔离或 metadata 过滤），并且**权限过滤必须在向量检索之前**，否则 RAG 会把
   不该被看到的规范带进回答。
