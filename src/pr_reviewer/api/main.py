"""FastAPI 服务（PRD §6.1）：异步任务 + 状态轮询 + diff hash 缓存（§6.5）。

工厂函数 create_app 支持依赖注入（测试用 FakeLLM）；模块级 app 供 uvicorn 使用。
"""
from __future__ import annotations

import uuid

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import PlainTextResponse

from pr_reviewer.cache import TTLCache
from pr_reviewer.config import Settings, get_settings
from pr_reviewer.log import get_logger
from pr_reviewer.models import PRReviewResult, ReviewRequest
from pr_reviewer.pipeline import run_review
from pr_reviewer.report import render_markdown
from pr_reviewer.sources import fetch_diff, load_contents


def create_app(settings: Settings | None = None, llm=None, retriever=None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="PR Smart Reviewer", version="0.1.0")
    tasks: dict[str, dict] = {}
    cache: TTLCache = TTLCache(settings.cache_ttl_hours)

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

    def _run_task(task_id: str, request: ReviewRequest) -> None:
        log = get_logger()
        tasks[task_id]["status"] = "running"
        # 只记标识与计数：源码正文、API key 一律不入日志（PRD §6.4）
        log.info("stage=task_start task=%s mode=%s", task_id, request.mode)
        try:
            diff, head_ref = fetch_diff(request, settings)
            contents = load_contents(request, head_ref,
                                     [f.file_path for f in diff.reviewable_files])
            result: PRReviewResult = run_review(settings, diff, contents, _llm(), _retriever())
            report = render_markdown(result)
            settings.reports_dir.mkdir(exist_ok=True)
            (settings.reports_dir / f"{task_id}.md").write_text(report, encoding="utf-8")
            tasks[task_id].update(status="completed", result=result, report=report)
            cache.put(TTLCache.make_key(request.model_dump()),
                      {"result": result, "report": report})
            log.info("stage=task_done task=%s issues=%d duration=%.2fs",
                     task_id, len(result.issues), result.duration_seconds)
        except Exception as exc:  # noqa: BLE001 — 任务级兜底，失败原因写入状态
            log.warning("stage=task_failed task=%s error_type=%s detail=%s",
                        task_id, type(exc).__name__, str(exc)[:200])
            tasks[task_id].update(status="failed", error=str(exc))

    @app.post("/review")
    def submit_review(request: ReviewRequest, background: BackgroundTasks):
        key = TTLCache.make_key(request.model_dump())
        cached = cache.get(key)
        task_id = uuid.uuid4().hex[:12]
        if cached is not None:
            tasks[task_id] = {"status": "completed", "result": cached["result"],
                              "report": cached["report"], "cached": True}
            return {"review_id": task_id, "status": "completed", "cached": True}
        tasks[task_id] = {"status": "queued"}
        background.add_task(_run_task, task_id, request)
        return {"review_id": task_id, "status": "queued"}

    @app.get("/review/{task_id}")
    def get_review(task_id: str):
        task = tasks.get(task_id)
        if not task:
            raise HTTPException(404, "review not found")
        return {"review_id": task_id, **task}

    @app.get("/review/{task_id}/report")
    def get_report(task_id: str):
        task = tasks.get(task_id)
        if not task:
            raise HTTPException(404, "review not found")
        if task["status"] != "completed":
            raise HTTPException(409, f"review is {task['status']}")
        return PlainTextResponse(task["report"], media_type="text/markdown")

    @app.get("/health")
    def health():
        probe = {"llm_backend": settings.llm_backend, "llm": None, "rag": None}
        try:
            probe["llm"] = _llm().health()
        except Exception as exc:  # noqa: BLE001
            probe["llm"] = f"error: {exc}"
        try:
            r = _retriever()
            probe["rag"] = bool(r and r._load())
        except Exception as exc:  # noqa: BLE001
            probe["rag"] = f"error: {exc}"
        return {"status": "ok", **probe}

    return app


app = create_app()
