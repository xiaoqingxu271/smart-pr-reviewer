# 检索门限扫描（真实 LLM）

- 模型：`agnes-2.5-flash`（openai），embedding：`BAAI/bge-base-zh-v1.5`，规范库 16 条款
- 每组跑完 7 个标注漏洞；margin 固定 0.05

| top1 门限 | 漏洞召回率 | 条款可追溯率 | 失败块 | token |
|---|---|---|---|---|
| off（无 RAG） | 0%（0/7）| 0% | 0 | 14,252 |
| 0.45 | 71%（5/7）| 100% | 0 | 17,678 |

## 逐 PR 明细（命中的规范条款）

### 档位 off（无 RAG）

| PR | 漏洞命中 | 引用条款 | 失败块 | 检出摘要 |
|---|---|---|---|---|
| pr-p01-route-not-gateway | 0/1 | （无） | 0 | [security] 缺少身份验证和授权检查：任何用户都可以通过猜测或枚举 uid 参数来访问任意用户的个人资料信息，存在不安全的直接对象引用（IDOR）漏洞。 |
| pr-p02-db-outside-repository | 0/1 | （无） | 0 | （未报问题） |
| pr-p03-internal-call-plain-requests | 0/1 | （无） | 0 | [security] 未验证 HTTP 响应状态码：直接使用 resp.json() 解析响应体，若服务端返回错误状态或非法 JSON 数据会导致异常或错误处理逻 |
| pr-p04-scheduler-thread | 0/1 | （无） | 0 | （未报问题） |
| pr-p05-money-float | 0/1 | （无） | 0 | （未报问题） |
| pr-p06-raw-exception | 0/1 | （无） | 0 | （未报问题） |
| pr-p07-cachekey-concat | 0/1 | （无） | 0 | （未报问题） |

### 档位 0.45

| PR | 漏洞命中 | 引用条款 | 失败块 | 检出摘要 |
|---|---|---|---|---|
| pr-p01-route-not-gateway | 1/1 | SEC-010 | 0 | [security] 使用 @router.get 直接暴露 HTTP 接口，绕过 gateway 统一鉴权/限流/链路追踪机制。 |
| pr-p02-db-outside-repository | 1/1 | SEC-011 | 0 | [security] Handler层直接创建数据库连接并执行SQL查询，违反了SEC-011规范。数据库连接与查询逻辑应收敛至repository层。 |
| pr-p03-internal-call-plain-requests | 1/1 | SEC-012 | 0 | [security] 使用 requests 直接拼内网地址调用服务，绕过 internal_client 的鉴权注入与服务发现。应改为 internal_cli |
| pr-p04-scheduler-thread | 1/1 | SEC-013 | 0 | [security] 代码使用 `threading.Thread` + `daemon=True` 启动常驻循环，违反 SEC-013。应改用 `schedul |
| pr-p05-money-float | 0/1 | SEC-015 | 0 | [security] 使用 `float` 进行金额累加并通过 `round` 收尾，存在浮点误差风险，违反 SEC-015。 |
| pr-p06-raw-exception | 1/1 | SEC-014 | 0 | [security] 裸抛 RuntimeError 违反 SEC-014：业务校验失败应使用 errors.raise_business() 抛出带业务错误码的 |
| pr-p07-cachekey-concat | 0/1 | SEC-016 | 0 | [security] 缓存键使用 f-string 拼接，违反 SEC-016 |

