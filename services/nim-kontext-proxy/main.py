import asyncio
import base64
import io
import logging
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import requests as _requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("nim-kontext-proxy")

NIM_KONTEXT_URL = os.environ.get("NIM_KONTEXT_URL", "http://flux-kontext:8000")
JOB_TTL = float(os.environ.get("JOB_TTL", "3600"))
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "/output"))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

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
    t0 = _now()
    logger.info("nim.request nim=%s steps=%s aspect=%s has_image=%s",
                NIM_KONTEXT_URL, body.get("steps"), body.get("aspect_ratio"),
                bool(body.get("image")))
    resp = _requests.post(f"{NIM_KONTEXT_URL}/v1/infer", json=body, timeout=300)
    resp.raise_for_status()
    data = resp.json()
    artifacts = data.get("artifacts", [])
    if not artifacts:
        raise ValueError(f"NIM returned no artifacts: {data}")
    b64 = artifacts[0].get("base64", "")
    if not b64:
        raise ValueError(f"NIM artifact missing base64: {artifacts[0]}")
    from PIL import Image as _Pil, ImageStat as _Stat
    raw = base64.b64decode(b64)
    pil_img = _Pil.open(io.BytesIO(raw)).convert("RGB")
    mean_brightness = sum(_Stat.Stat(pil_img).mean) / 3.0
    buf = io.BytesIO()
    pil_img.save(buf, format="PNG")
    png = buf.getvalue()
    logger.info("nim.done nim_ms=%d png_kb=%d brightness=%.1f",
                int((_now() - t0) * 1000), len(png) // 1024, mean_brightness)
    if len(png) < 10 * 1024 or mean_brightness < 5.0:
        raise ValueError(
            f"NIM returned degenerate image (size={len(png)}B brightness={mean_brightness:.1f}) "
            "– possible Blackwell TRT warm-up failure; retry the request"
        )
    return png


async def _worker() -> None:
    loop = asyncio.get_running_loop()
    while True:
        job_id = await _queue.get()
        job = _jobs.get(job_id)
        if job is None:
            _queue.task_done()
            continue
        queue_wait_ms = int((_now() - job["created"]) * 1000)
        job["status"] = "running"
        job["updated"] = _now()
        run = job.pop("_run", None)
        logger.info("job.started id=%s queue_wait_ms=%d queue_depth=%d",
                    job_id[:8], queue_wait_ms, _queue.qsize())
        t0 = _now()
        try:
            png = await loop.run_in_executor(_executor, run)
            job["result"] = png
            job["status"] = "done"
            out_path = OUTPUT_DIR / f"{job_id}.png"
            out_path.write_bytes(png)
            logger.info("job.done id=%s total_ms=%d saved=%s", job_id[:8], int((_now() - t0) * 1000), out_path)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            job["status"] = "error"
            job["error"] = f"{type(e).__name__}: {e}"
            logger.error("job.error id=%s error=%r", job_id[:8], job["error"])
        finally:
            job["updated"] = _now()
            _queue.task_done()
            _sweep()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _queue
    _queue = asyncio.Queue()
    worker = asyncio.create_task(_worker())
    logger.info("startup nim_kontext_url=%s job_ttl=%ss", NIM_KONTEXT_URL, int(JOB_TTL))
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
    pos = _queue_position(job["id"])
    logger.info("job.queued id=%s prompt=%r has_image=%s ratio=%s steps=%s queue_pos=%d",
                job["id"][:8], req.prompt[:60], bool(req.image),
                req.aspect_ratio, req.steps, pos)
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
    logger.info("job.result id=%s size_kb=%d", job_id[:8], len(job["result"]) // 1024)
    return Response(content=job["result"], media_type="image/png")


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "nim_kontext_url": NIM_KONTEXT_URL,
        "queue_depth": _queue.qsize() if _queue is not None else 0,
        "jobs_tracked": len(_jobs),
    }
