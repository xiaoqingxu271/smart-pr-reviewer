# 检索门限扫描（真实 LLM）

- 模型：`agnes-2.5-flash`（openai），embedding：`BAAI/bge-base-zh-v1.5`，规范库 16 条款
> ⚠️ 有审查失败的块（多为限流/超时），该组数字被低估，需重跑。
- 每组跑完 7 个标注漏洞；margin 固定 0.05

| 档位 | 漏洞召回率 | 其中严格口径 | 其中引用口径 | 条款可追溯率 | 失败块 | token |
|---|---|---|---|---|---|---|
| off（无 RAG） | 0%（0/7）| 0/7 | 0/7 | 0% | 0 | 14,024 |
| 0.45 | 86%（6/7）| 5/7 | 6/7 | 100% | 1 | 16,185 |

## 逐 PR 明细（命中的规范条款）

### 档位 off（无 RAG）

| PR | 漏洞命中 | 引用条款 | 失败块 | 检出摘要 |
|---|---|---|---|---|
| pr-p01-route-not-gateway | 0/1 | （无） | 0 | [security] 缺少认证和授权检查：端点允许任何用户通过uid直接访问其他用户的个人资料，未验证请求者身份及访问权限。 |
| pr-p02-db-outside-repository | 0/1 | （无） | 0 | [security] 数据库路径硬编码为相对路径 'orders.db'，攻击者可能通过路径操作或配置篡改指定非预期数据库位置，导致数据泄露或被污染。 |
| pr-p03-internal-call-plain-requests | 0/1 | （无） | 0 | [security] SSRF - host parameter is directly interpolated into the URL without an |
| pr-p04-scheduler-thread | 0/1 | （无） | 0 | （未报问题） |
| pr-p05-money-float | 0/1 | （无） | 0 | （未报问题） |
| pr-p06-raw-exception | 0/1 | （无） | 0 | （未报问题） |
| pr-p07-cachekey-concat | 0/1 | （无） | 0 | [security] Potential cache key injection: user_id and tenant values are directly  |

### 档位 0.45

| PR | 漏洞命中 | 引用条款 | 失败块 | 检出摘要 |
|---|---|---|---|---|
| pr-p01-route-not-gateway | 1/1 | SEC-010 | 0 | [security] HTTP 接口使用框架装饰器 `@router.get` 直接暴露，应改用 `gateway.route()` 注册以统一注入鉴权、限流与链 |
| pr-p02-db-outside-repository | 1/1 | SEC-011 | 0 | [security] 业务代码（handler）直接在函数内创建数据库连接并执行 SQL，应通过 repository 层进行数据访问。 |
| pr-p03-internal-call-plain-requests | 1/1 | SEC-012 | 0 | [security] 直接调用 requests.get 访问内部服务，未通过 internal_client 进行调用，绕过内部鉴权头注入、重试机制和服务发现地 |
| pr-p04-scheduler-thread | 1/1 | SEC-013 | 0 | [security] 使用了自建 threading.Thread + while True 的常驻循环进行周期性任务，违反 SEC-013。应使用 schedu; [security] 自定义 while True + time.sleep 常驻循环未通过 scheduler 注册，违反周期性任务管理规范。 |
| pr-p05-money-float | 1/1 | SEC-015 | 0 | [security] 使用 float 累加金额并 round 收尾，违反 SEC-015。浮点数累加会累积分位误差，导致对账出现无法解释的差额。 |
| pr-p06-raw-exception | 1/1 | SEC-014 | 0 | [security] 裸抛 RuntimeError，未使用 errors.raise_business() 抛出带业务错误码的异常，会被兜底成 500 |
| pr-p07-cachekey-concat | 0/1 | （无） | 1 | （未报问题） |


显著性（Fisher 单侧精确检验，0.45 vs off）：**p = 0.0023**，0.05 水平下显著
