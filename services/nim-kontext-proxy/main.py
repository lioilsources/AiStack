import asyncio
import base64
import io
import logging
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Optional

import requests as _requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

logger = logging.getLogger("nim-kontext-proxy")

NIM_KONTEXT_URL = os.environ.get("NIM_KONTEXT_URL", "http://ai-flux-kontext-nim:8000")
JOB_TTL = float(os.environ.get("JOB_TTL", "3600"))

_jobs: dict[str, dict] = {}
_queue: "asyncio.Queue[str]" = None
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="nim")


def _now() -> float:
    return time.time()


def _new_job() -> dict:
    job = {
        "id": uuid.uuid4().hex,
        "status": "queued",
        "created": _now(),
        "updated": _now(),
        "result": None,
        "error": None,
    }
    _jobs[job["id"]] = job
    return job


def _queue_position(job_id: str) -> int:
    pending = sorted(
        (j for j in _jobs.values() if j["status"] == "queued"),
        key=lambda j: j["created"],
    )
    for i, j in enumerate(pending):
        if j["id"] == job_id:
            return i
    return 0


def _accepted(job: dict) -> dict:
    return {
        "id": job["id"],
        "status": job["status"],
        "status_url": f"/nim/flux-kontext/jobs/{job['id']}",
        "queue_position": _queue_position(job["id"]),
    }


def _public(job: dict) -> dict:
    out = {"id": job["id"], "status": job["status"], "step": 0, "total": 0}
    if job["status"] == "queued":
        out["queue_position"] = _queue_position(job["id"])
    elif job["status"] == "done":
        out["result_url"] = f"/nim/flux-kontext/jobs/{job['id']}/result"
    elif job["status"] == "error":
        out["error"] = job["error"]
    return out


def _sweep() -> None:
    cutoff = _now() - JOB_TTL
    stale = [
        jid for jid, j in _jobs.items()
        if j["status"] in ("done", "error") and j["updated"] < cutoff
    ]
    for jid in stale:
        _jobs.pop(jid, None)


def _call_nim(body: dict) -> bytes:
    resp = _requests.post(f"{NIM_KONTEXT_URL}/v1/infer", json=body, timeout=300)
    resp.raise_for_status()
    data = resp.json()
    artifacts = data.get("artifacts", [])
    if not artifacts:
        raise ValueError(f"NIM Kontext returned no artifacts: {data}")
    b64 = artifacts[0].get("base64", "")
    if not b64:
        raise ValueError(f"NIM Kontext artifact missing base64: {artifacts[0]}")
    from PIL import Image as _Pil
    raw = base64.b64decode(b64)
    buf = io.BytesIO()
    _Pil.open(io.BytesIO(raw)).convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


async def _worker() -> None:
    loop = asyncio.get_running_loop()
    while True:
        job_id = await _queue.get()
        job = _jobs.get(job_id)
        if job is None:
            _queue.task_done()
            continue
        job["status"] = "running"
        job["updated"] = _now()
        run = job.pop("_run", None)
        try:
            job["result"] = await loop.run_in_executor(_executor, run)
            job["status"] = "done"
        except asyncio.CancelledError:
            raise
        except Exception as e:
            job["status"] = "error"
            job["error"] = f"{type(e).__name__}: {e}"
            logger.exception("job %s failed", job_id)
        finally:
            job["updated"] = _now()
            _queue.task_done()
            _sweep()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _queue
    _queue = asyncio.Queue()
    worker = asyncio.create_task(_worker())
    yield
    worker.cancel()
    try:
        await worker
    except asyncio.CancelledError:
        pass


app = FastAPI(title="nim-kontext-proxy", lifespan=lifespan)


class InferRequest(BaseModel):
    prompt: str
    image: Optional[str] = None
    aspect_ratio: str = "1:1"
    cfg_scale: float = 3.5
    steps: int = 30
    seed: Optional[int] = None


@app.post("/nim/flux-kontext/v1/infer", status_code=202)
async def infer(req: InferRequest):
    body = {
        "prompt": req.prompt,
        "aspect_ratio": req.aspect_ratio,
        "cfg_scale": req.cfg_scale,
        "steps": req.steps,
    }
    if req.seed is not None:
        body["seed"] = req.seed
    if req.image is not None:
        body["image"] = req.image
    job = _new_job()
    job["_run"] = lambda: _call_nim(body)
    await _queue.put(job["id"])
    return _accepted(job)


@app.get("/nim/flux-kontext/jobs/{job_id}")
async def job_status(job_id: str):
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found (unknown id or expired)")
    return _public(job)


@app.get("/nim/flux-kontext/jobs/{job_id}/result")
async def job_result(job_id: str):
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found (unknown id or expired)")
    if job["status"] != "done":
        raise HTTPException(409, f"result not ready (status: {job['status']})")
    return Response(content=job["result"], media_type="image/png")


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "nim_kontext_url": NIM_KONTEXT_URL,
        "queue_depth": _queue.qsize() if _queue is not None else 0,
        "jobs_tracked": len(_jobs),
    }
