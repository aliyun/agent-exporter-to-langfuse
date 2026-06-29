from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pathlib import Path

from src.config import Config, read_version
from src.queue import JobQueue
from src.store import Store


def create_app(config: Config, store: Store | None = None,
               queue: JobQueue | None = None) -> FastAPI:
    app = FastAPI(title="langstash-tester", docs_url=None, redoc_url=None)

    _store = store
    _queue = queue

    def _get_store() -> Store:
        assert _store is not None
        return _store

    def _get_queue() -> JobQueue:
        assert _queue is not None
        return _queue

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse({"status": "ok", "version": read_version()})

    @app.post("/e2e/jobs")
    async def create_job(request: Request) -> JSONResponse:
        body = await request.json()

        branch = body.get("branch")
        if not branch:
            return JSONResponse({"error": "branch is required"}, status_code=400)

        s = _get_store()
        q = _get_queue()

        job = s.create_job(
            branch=branch,
            commit=body.get("commit"),
            mode=body.get("mode", "branch"),
            test_command=body.get("test_command"),
            timeout_seconds=body.get("timeout_seconds", config.e2e.default_timeout_seconds),
            callback_url=body.get("callback_url"),
            metadata=body.get("metadata"),
        )

        position = q.enqueue(job["job_id"], branch)
        if position is None:
            s.update_job(job["job_id"], status="cancelled")
            return JSONResponse(
                {"error": f"branch '{branch}' already has a pending or running job (policy: reject)"},
                status_code=409,
            )

        return JSONResponse({
            "job_id": job["job_id"],
            "status": "pending",
            "created_at": job["created_at"],
            "position": position,
        }, status_code=202)

    @app.get("/e2e/jobs/{job_id}")
    async def get_job(job_id: str) -> JSONResponse:
        job = _get_store().get_job(job_id)
        if job is None:
            return JSONResponse({"error": "job not found"}, status_code=404)
        return JSONResponse(job)

    @app.get("/e2e/jobs")
    async def list_jobs(
        status: str | None = Query(None),
        branch: str | None = Query(None),
        limit: int = Query(20, ge=1, le=100),
    ) -> JSONResponse:
        jobs = _get_store().list_jobs(status=status, branch=branch, limit=limit)
        return JSONResponse(jobs)

    @app.post("/e2e/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str) -> JSONResponse:
        s = _get_store()
        job = s.get_job(job_id)
        if job is None:
            return JSONResponse({"error": "job not found"}, status_code=404)

        if job["status"] not in ("pending", "running"):
            return JSONResponse({"error": f"cannot cancel job in status '{job['status']}'"}, status_code=409)

        q = _get_queue()
        cancelled = q.cancel(job_id)
        if cancelled or job["status"] == "pending":
            s.update_job(job_id, status="cancelled")

        return JSONResponse({"job_id": job_id, "status": "cancelled"})

    @app.get("/e2e/jobs/{job_id}/logs")
    async def get_logs(job_id: str) -> PlainTextResponse:
        base_log_dir = Path(config.storage.log_dir).resolve()
        log_path = (base_log_dir / f"{job_id}.log").resolve()
        try:
            log_path.relative_to(base_log_dir)
        except ValueError:
            return PlainTextResponse("invalid job id", status_code=400)

        s = _get_store()
        job = s.get_job(job_id)
        if job is None:
            return PlainTextResponse("job not found", status_code=404)

        base_log_dir = Path(config.storage.log_dir).resolve()
        log_path = (base_log_dir / f"{job_id}.log").resolve()
        try:
            log_path.relative_to(base_log_dir)
        except ValueError:
            return PlainTextResponse("invalid job id", status_code=400)

        if not log_path.exists():
            return PlainTextResponse("", status_code=200)

        content = log_path.read_text(encoding="utf-8", errors="replace")
        return PlainTextResponse(content)

    return app
