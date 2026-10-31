"""FastAPI 服务（PRD §6.1）：异步任务 + 状态轮询 + diff hash 缓存（§6.5）。

落地要点（都是踩过之后补的）：
- 任务态与结果缓存落在 SQLite，进程重启和多 worker 都能查到 review_id；
- 缓存 key 是 **diff 内容 + head**，不是分支名——否则 force-push 后 24 小时内
  会把上一版代码的报告当新版返回；
- 审查接口一律要求 Bearer token，未配置就 503 停用（fail-closed）：报告里含源码
  片段，裸奔等于开一条源码外泄通道；
- 返回给调用方的错误是友好文案，异常细节只进服务端日志。

工厂函数 create_app 支持依赖注入（测试用 FakeLLM）；模块级 app 供 uvicorn 使用。
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse

from pr_reviewer.config import Settings, get_settings
from pr_reviewer.log import get_logger
from pr_reviewer.models import PRReviewResult, ReviewRequest
from pr_reviewer.pipeline import run_review
from pr_reviewer.publishing import GitHubPublisher
from pr_reviewer.report import render_markdown
from pr_reviewer.sources import RepoNotAllowed, fetch_diff, load_contents
from pr_reviewer.store import ResultCache, TaskStore
from pr_reviewer.tasks import QueueFull, TaskRunner
from pr_reviewer.webhooks import WebhookPayloadInvalid, WebhookRejected, parse_webhook

GENERIC_FAILURE = "审查未完成：服务端内部错误，请携带 review_id 联系管理员"


def create_app(settings: Settings | None = None, llm=None, retriever=None,
               publisher=None) -> FastAPI:
    settings = settings or get_settings()
    runner = TaskRunner(settings.max_concurrent_reviews, settings.max_pending_reviews)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        runner.shutdown(wait=False)          # 进程退出别让孤儿线程继续打 LLM

    app = FastAPI(title="PR Smart Reviewer", version="0.1.0", lifespan=lifespan)
    store = TaskStore(settings.db_path)
    cache = ResultCache(settings.db_path, settings.cache_ttl_hours)

    def _llm():
        if llm is not None:
            return llm
        from pr_reviewer.llm import get_backend
        return get_backend(settings)

    def _retriever():
        if retriever is not None:
            return retriever
        try:
            from pr_reviewer.rag import StandardsRetriever
            return StandardsRetriever(settings)
        except Exception:  # noqa: BLE001 — ChromaDB 不可用 → 无 RAG 降级（PRD §6.3）
            return None

    def _publisher():
        """结果写回 PR 的发布器；没配 token 就是 None（只出报告，不发评论）。"""
        if publisher is not None:
            return publisher
        return GitHubPublisher(settings.github_token, settings.github_api_base) \
            if settings.github_token else None

    def require_token(authorization: str | None = Header(default=None)) -> None:
        if not settings.api_token:
            raise HTTPException(503, "审查接口未启用：服务端需配置 API_TOKEN")
        if authorization != f"Bearer {settings.api_token}":
            raise HTTPException(401, "Authorization 缺失或不正确")

    def _run_task(task_id: str, request: ReviewRequest) -> None:
        log = get_logger()
        store.update(task_id, status="running")
        log.info("stage=task_start task=%s mode=%s", task_id, request.mode)
        try:
            diff, head_ref = fetch_diff(request, settings)
            key = ResultCache.make_key(diff.raw_diff or "", str(head_ref))
            hit = cache.get(key)
            if hit is not None:
                store.update(task_id, status="completed", result=hit["result"],
                             report=hit["report"], cached=True)
                log.info("stage=task_cached task=%s", task_id)
                return

            contents = load_contents(request, head_ref,
                                     [f.file_path for f in diff.reviewable_files], settings)
            result: PRReviewResult = run_review(settings, diff, contents, _llm(), _retriever())
            report = render_markdown(result)
            payload = result.model_dump(mode="json")
            settings.reports_dir.mkdir(parents=True, exist_ok=True)
            (settings.reports_dir / f"{task_id}.md").write_text(report, encoding="utf-8")
            store.update(task_id, status="completed", result=payload, report=report)
            cache.put(key, payload, report)
            log.info("stage=task_done task=%s issues=%d duration=%.2fs",
                     task_id, len(result.issues), result.duration_seconds)
            if request.post_comments:
                # 缓存命中路径不发评论：key 含 head sha，同一 sha 说明评论已经发过了。
                # 发布失败只记日志——审查结果已落库，不该因为写评论失败就把任务判失败
                pub = _publisher()
                if pub is None:
                    log.warning("stage=publish_skipped task=%s reason=no_github_token",
                                task_id)
                else:
                    try:
                        stats = pub.publish(request.repo_url, request.pr_number,
                                            str(head_ref), result)
                        log.info("stage=published task=%s inline=%d deferred=%d summary=%s",
                                 task_id, stats["inline"], len(stats["deferred"]),
                                 stats["summary_posted"])
                    except Exception as exc:  # noqa: BLE001
                        log.warning("stage=publish_failed task=%s error_type=%s detail=%s",
                                    task_id, type(exc).__name__, str(exc)[:200])
        except RepoNotAllowed as exc:
            store.update(task_id, status="failed", error=str(exc))
            log.warning("stage=task_rejected task=%s reason=%s", task_id, exc)
        except Exception as exc:  # noqa: BLE001 — 任务级兜底：细节进日志，结论给调用方
            store.update(task_id, status="failed", error=GENERIC_FAILURE)
            log.warning("stage=task_failed task=%s error_type=%s detail=%s",
                        task_id, type(exc).__name__, str(exc)[:300])

    @app.post("/review", dependencies=[Depends(require_token)])
    def submit_review(request: ReviewRequest):
        task_id = uuid.uuid4().hex[:12]
        store.create(task_id, request.model_dump(exclude_none=True))
        try:
            runner.submit(_run_task, task_id, request)
        except QueueFull as exc:
            store.delete(task_id)            # 不留一条永远不会动的 queued 记录
            raise HTTPException(429, str(exc)) from exc
        return {"review_id": task_id, "status": "queued"}

    @app.post("/webhook/github")
    async def github_webhook(request: Request):
        """GitHub PR 事件入口。鉴权靠 HMAC 签名，因此不走 require_token。

        先读原始字节再验签：验签通过前不解析、不信任内容。
        """
        raw = await request.body()
        if not settings.webhook_secret:
            raise HTTPException(503, "Webhook 未启用：服务端需配置 WEBHOOK_SECRET")
        try:
            review = parse_webhook(settings.webhook_secret, raw,
                                   request.headers.get("x-hub-signature-256"),
                                   request.headers.get("x-github-event"))
        except WebhookRejected:
            raise HTTPException(403, "签名校验失败") from None
        except WebhookPayloadInvalid as exc:
            raise HTTPException(400, str(exc)) from exc
        if review is None:
            return {"ignored": True}
        if not _publisher():
            # 没人收结果的事件不必消耗 LLM 配额：webhook 的价值就是写回 PR
            return {"ignored": True, "reason": "未配置 GITHUB_TOKEN，无法写回 PR"}
        task_id = uuid.uuid4().hex[:12]
        store.create(task_id, review.model_dump(exclude_none=True))
        try:
            runner.submit(_run_task, task_id, review)
        except QueueFull as exc:
            store.delete(task_id)
            raise HTTPException(429, str(exc)) from exc
        return {"review_id": task_id, "status": "queued"}

    @app.get("/review/{task_id}", dependencies=[Depends(require_token)])
    def get_review(task_id: str):
        task = store.get(task_id)
        if not task:
            raise HTTPException(404, "review not found")
        return {k: task[k] for k in ("review_id", "status", "result", "error", "cached")}

    @app.get("/review/{task_id}/report", dependencies=[Depends(require_token)])
    def get_report(task_id: str):
        task = store.get(task_id)
        if not task:
            raise HTTPException(404, "review not found")
        if task["status"] != "completed":
            raise HTTPException(409, f"review is {task['status']}")
        return PlainTextResponse(task["report"], media_type="text/markdown")

    @app.get("/health")
    def health():
        """探活不需要 token（部署健康检查用），但只回状态不回内容。"""
        probe = {"llm_backend": settings.llm_backend, "llm": None, "rag": None,
                 "auth_configured": bool(settings.api_token),
                 "local_mode_allowed": bool(settings.allowed_repo_roots.strip()),
                 "webhook_configured": bool(settings.webhook_secret),
                 "publish_enabled": bool(settings.github_token or publisher is not None),
                 "load": runner.stats, "db": None}
        try:
            probe["llm"] = _llm().health()
        except Exception as exc:  # noqa: BLE001
            probe["llm"] = f"error: {type(exc).__name__}"
        try:
            r = _retriever()
            size = r.size() if r else 0
            # available 以"索引真的有内容"为准：空索引就是检索空转，正是最初的故障形态
            probe["rag"] = {"available": bool(r and size), "index_size": size}
        except Exception as exc:  # noqa: BLE001
            probe["rag"] = {"available": False, "error": type(exc).__name__}
        try:
            probe["db"] = store.conn_ok()
        except Exception as exc:  # noqa: BLE001
            probe["db"] = f"error: {type(exc).__name__}"
        return {"status": "ok", **probe}

    return app


app = create_app()
