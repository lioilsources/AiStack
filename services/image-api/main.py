import asyncio
import base64
import io
import logging
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Literal, Optional

import torch
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

logger = logging.getLogger("image-api")

flux_pipe = None
qwen_pipe = None

# Whether the NSFW LoRA adapter successfully attached to flux_pipe.
flux_lora_loaded = False

# Adapter name + default strength for the NSFW LoRA (Ryouko65777/Flux-Uncensored-V2).
NSFW_ADAPTER = "nsfw"
DEFAULT_LORA_SCALE = float(os.environ.get("FLUX_NSFW_LORA_SCALE", "1.0"))

# ── Async job store ───────────────────────────────────────────
# Image generation takes 1–3 min, which is longer than Cloudflare's 100 s
# proxy timeout (→ 524) and is fragile over mobile networks. So requests
# don't block: POST enqueues a job and returns a job_id immediately; the
# client polls GET /v1/images/jobs/{id} for progress and the result.
#
# A single background worker pulls jobs from an asyncio.Queue and runs the
# (blocking) diffusion call in a 1-thread executor. That serializes GPU work
# AND keeps the event loop free, so /health and polling stay responsive
# during generation.
JOB_TTL = float(os.environ.get("JOB_TTL", "3600"))  # keep finished jobs this long (s)

_jobs: dict[str, dict] = {}
_queue: "asyncio.Queue[str]" = None  # created in lifespan (needs running loop)
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gpu")


def _b64_to_pil(data: str):
    from PIL import Image
    try:
        return Image.open(io.BytesIO(base64.b64decode(data))).convert("RGB")
    except Exception:
        raise HTTPException(400, "invalid base64 image data")


def _pil_to_png(img) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _attach_flux_lora(pipe) -> None:
    """Load the NSFW LoRA as a named adapter, kept DISABLED so SFW is the default.

    Requests opt in per-call (nsfw=True), which toggles the adapter on the single
    GPU worker thread — see _set_flux_variant. Missing file or load failure just
    leaves the nsfw variant unavailable; SFW generation is unaffected.
    """
    global flux_lora_loaded

    if os.environ.get("LOAD_FLUX_LORA", "1") != "1":
        return
    lora_path = os.environ.get(
        "FLUX_NSFW_LORA", "/root/.cache/huggingface/flux/lora/lora.safetensors"
    )
    if not os.path.exists(lora_path):
        logger.warning("NSFW LoRA not found at %s — nsfw variant disabled "
                       "(run scripts/download_flux_lora.sh)", lora_path)
        return
    try:
        pipe.load_lora_weights(
            os.path.dirname(lora_path),
            weight_name=os.path.basename(lora_path),
            adapter_name=NSFW_ADAPTER,
        )
        pipe.disable_lora()  # SFW is the default; requests enable per-call
        flux_lora_loaded = True
        logger.info("loaded NSFW LoRA '%s' from %s (disabled by default)",
                    NSFW_ADAPTER, lora_path)
    except Exception:  # noqa: BLE001 — never let a bad LoRA take down SFW generation
        logger.exception("failed to load NSFW LoRA from %s — nsfw variant disabled", lora_path)


def _load_flux():
    from diffusers import FluxPipeline

    model_id = os.environ["FLUX_MODEL_ID"]
    cache_dir = "/root/.cache/huggingface/flux"

    if os.environ.get("FLUX_FP8", "0") == "1":
        from optimum.quanto import freeze, qfloat8, quantize
        p = FluxPipeline.from_pretrained(model_id, torch_dtype=torch.bfloat16, cache_dir=cache_dir)
        quantize(p.transformer, weights=qfloat8)
        freeze(p.transformer)
        quantize(p.text_encoder_2, weights=qfloat8)
        freeze(p.text_encoder_2)
        # LoRA can't attach to a quanto-frozen transformer; nsfw stays unavailable under FP8.
        return p.to("cuda")

    pipe = FluxPipeline.from_pretrained(
        model_id, torch_dtype=torch.bfloat16, cache_dir=cache_dir
    ).to("cuda")
    _attach_flux_lora(pipe)
    return pipe


def _set_flux_variant(nsfw: bool, scale) -> None:
    """Toggle the NSFW LoRA on flux_pipe. Safe because the GPU worker is single
    threaded (max_workers=1), so no two generations race on adapter state."""
    if not flux_lora_loaded:
        return  # request endpoint already rejected nsfw=True when unavailable
    if nsfw:
        flux_pipe.set_adapters(NSFW_ADAPTER, adapter_weights=scale or DEFAULT_LORA_SCALE)
        flux_pipe.enable_lora()
    else:
        flux_pipe.disable_lora()


def _load_qwen_image():
    from diffusers import QwenImageEditPlusPipeline

    model_id = os.environ["QWEN_IMAGE_MODEL_ID"]
    cache_dir = "/root/.cache/huggingface/qwen-image"
    # Support both layouts: HF hub cache (models--org--name/snapshots/...) and a direct
    # snapshot folder (model_index.json at the cache root, as produced by `git clone`).
    source = cache_dir if os.path.exists(os.path.join(cache_dir, "model_index.json")) else model_id
    return QwenImageEditPlusPipeline.from_pretrained(
        source, torch_dtype=torch.bfloat16, local_files_only=True, cache_dir=cache_dir
    ).to("cuda")


# ── Job helpers ───────────────────────────────────────────────

def _now() -> float:
    return time.time()


def _new_job(kind: str, total: int) -> dict:
    job = {
        "id": uuid.uuid4().hex,
        "kind": kind,
        "status": "queued",   # queued → running → done | error
        "step": 0,
        "total": total,
        "created": _now(),
        "updated": _now(),
        "result": None,       # list[bytes] (raw PNG, one per image) when done
        "error": None,
    }
    _jobs[job["id"]] = job
    return job


def _queue_position(job_id: str) -> int:
    """0 = next up. Counts queued jobs created before this one."""
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
        "status_url": f"/v1/images/jobs/{job['id']}",
        "queue_position": _queue_position(job["id"]),
    }


def _public(job: dict) -> dict:
    out = {
        "id": job["id"],
        "status": job["status"],
        "step": job["step"],
        "total": job["total"],
        "created": int(job["created"]),
    }
    if job["status"] == "queued":
        out["queue_position"] = _queue_position(job["id"])
    elif job["status"] == "done":
        # Keep the poll response tiny — the image (MBs) is fetched ONCE from
        # result_url, not re-sent on every poll (that re-download over mobile
        # is what pushed polls past Cloudflare's 100 s timeout → 524).
        out["count"] = len(job["result"])
        out["result_url"] = f"/v1/images/jobs/{job['id']}/result"
    elif job["status"] == "error":
        out["error"] = job["error"]
    return out


def _sweep() -> None:
    """Drop finished jobs older than JOB_TTL so the store can't grow unbounded."""
    cutoff = _now() - JOB_TTL
    stale = [
        jid for jid, j in _jobs.items()
        if j["status"] in ("done", "error") and j["updated"] < cutoff
    ]
    for jid in stale:
        _jobs.pop(jid, None)


def _run_pipe(pipe, job, **kwargs):
    """Run a diffusers pipeline, reporting per-step progress onto the job.

    callback_on_step_end runs in the worker thread; updating plain dict fields
    is safe under the GIL. Falls back gracefully if a pipeline rejects the kwarg.
    """
    def _cb(p, step, timestep, callback_kwargs):
        job["step"] = step + 1
        job["updated"] = _now()
        return callback_kwargs

    try:
        return pipe(callback_on_step_end=_cb, **kwargs)
    except TypeError as e:
        if "callback" not in str(e):
            raise
        return pipe(**kwargs)


def _run_generation(job, prompt, w, h, n, steps, nsfw=False, scale=None) -> list[bytes]:
    _set_flux_variant(nsfw, scale)
    result = _run_pipe(
        flux_pipe, job,
        prompt=prompt,
        width=w,
        height=h,
        num_images_per_prompt=n,
        num_inference_steps=steps,
        guidance_scale=3.5,
    )
    return [_pil_to_png(img) for img in result.images]


def _run_edit(job, prompt, image, w, h, n, steps) -> list[bytes]:
    result = _run_pipe(
        qwen_pipe, job,
        prompt=prompt,
        image=image,
        width=w,
        height=h,
        num_images_per_prompt=n,
        num_inference_steps=steps,
    )
    images = result.images if hasattr(result, "images") else result
    return [_pil_to_png(img) for img in images]


async def _worker() -> None:
    loop = asyncio.get_running_loop()
    while True:
        job_id = await _queue.get()
        job = _jobs.get(job_id)
        if job is None:  # expired before it ran
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
        except Exception as e:  # noqa: BLE001 — surface any inference failure to the client
            job["status"] = "error"
            job["error"] = f"{type(e).__name__}: {e}"
            logger.exception("job %s failed", job_id)
        finally:
            job["updated"] = _now()
            _queue.task_done()
            _sweep()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global flux_pipe, qwen_pipe, _queue

    if os.environ.get("LOAD_FLUX", "1") == "1":
        flux_pipe = _load_flux()

    if os.environ.get("LOAD_QWEN_IMAGE", "1") == "1":
        qwen_pipe = _load_qwen_image()

    _queue = asyncio.Queue()
    worker = asyncio.create_task(_worker())

    yield

    worker.cancel()
    try:
        await worker
    except asyncio.CancelledError:
        pass
    if flux_pipe is not None:
        del flux_pipe
    if qwen_pipe is not None:
        del qwen_pipe
    torch.cuda.empty_cache()


app = FastAPI(title="image-api", lifespan=lifespan)


class GenerationRequest(BaseModel):
    prompt: str
    n: int = Field(1, ge=1, le=4)
    size: str = "1024x1024"
    response_format: Literal["b64_json"] = "b64_json"
    model: Optional[str] = "flux-1-dev"
    quality: Optional[str] = "standard"
    num_inference_steps: Optional[int] = None
    # NSFW LoRA toggle. Unset → default to NSFW when the LoRA is loaded (else SFW);
    # explicit false → SFW; explicit true → require the LoRA (503 if unavailable).
    nsfw: Optional[bool] = None
    lora_scale: Optional[float] = Field(None, ge=0.0, le=2.0)  # override LoRA strength


class EditRequest(BaseModel):
    image: str                       # base64-encoded PNG/JPEG
    prompt: str
    n: int = Field(1, ge=1, le=4)
    size: str = "1024x1024"
    response_format: Literal["b64_json"] = "b64_json"
    model: Optional[str] = "qwen-image-edit"
    num_inference_steps: Optional[int] = None


def _parse_size(size: str) -> tuple[int, int]:
    try:
        w, h = size.split("x")
        return int(w), int(h)
    except (ValueError, AttributeError):
        raise HTTPException(400, f"invalid size '{size}', expected WxH e.g. 1024x1024")


@app.post("/v1/images/generations", status_code=202)
async def generate(req: GenerationRequest):
    if flux_pipe is None:
        raise HTTPException(503, "FLUX pipeline not loaded (set LOAD_FLUX=1)")
    if req.nsfw is True and not flux_lora_loaded:
        raise HTTPException(
            503,
            "NSFW variant unavailable: LoRA not loaded "
            "(run scripts/download_flux_lora.sh and set LOAD_FLUX_LORA=1, FLUX_FP8=0)",
        )
    # Default to NSFW when the LoRA is available; nsfw=false still forces SFW.
    nsfw = flux_lora_loaded if req.nsfw is None else req.nsfw
    w, h = _parse_size(req.size)
    steps = req.num_inference_steps or (20 if req.quality == "hd" else 14)
    job = _new_job("generation", steps)
    job["_run"] = lambda: _run_generation(
        job, req.prompt, w, h, req.n, steps, nsfw, req.lora_scale
    )
    await _queue.put(job["id"])
    return _accepted(job)


@app.post("/v1/images/edits", status_code=202)
async def edit(req: EditRequest):
    if qwen_pipe is None:
        raise HTTPException(503, "Qwen image pipeline not loaded (set LOAD_QWEN_IMAGE=1)")
    w, h = _parse_size(req.size)
    steps = req.num_inference_steps or 20
    image = _b64_to_pil(req.image)  # validate now → fail fast with 400, not async
    job = _new_job("edit", steps)
    job["_run"] = lambda: _run_edit(job, req.prompt, image, w, h, req.n, steps)
    await _queue.put(job["id"])
    return _accepted(job)


@app.get("/v1/images/jobs/{job_id}")
async def job_status(job_id: str):
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found (unknown id or expired)")
    return _public(job)


@app.get("/v1/images/jobs/{job_id}/result")
async def job_result(job_id: str, index: int = 0):
    """Fetch a finished image as raw PNG bytes. Fetch this ONCE per job —
    don't poll it (it's MBs). For n>1, select with ?index=N."""
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found (unknown id or expired)")
    if job["status"] != "done":
        raise HTTPException(409, f"result not ready (status: {job['status']})")
    if not 0 <= index < len(job["result"]):
        raise HTTPException(404, f"index {index} out of range (0..{len(job['result']) - 1})")
    return Response(content=job["result"][index], media_type="image/png")


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "flux_loaded": flux_pipe is not None,
        "flux_nsfw_lora_loaded": flux_lora_loaded,
        "qwen_image_loaded": qwen_pipe is not None,
        "queue_depth": _queue.qsize() if _queue is not None else 0,
        "jobs_tracked": len(_jobs),
    }
