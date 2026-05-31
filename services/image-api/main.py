import base64
import io
import os
import time
from contextlib import asynccontextmanager
from typing import Literal, Optional

import torch
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

flux_pipe = None
qwen_pipe = None


def _b64_to_pil(data: str):
    from PIL import Image
    return Image.open(io.BytesIO(base64.b64decode(data))).convert("RGB")


def _pil_to_b64(img) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


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
        return p.to("cuda")

    return FluxPipeline.from_pretrained(
        model_id, torch_dtype=torch.bfloat16, cache_dir=cache_dir
    ).to("cuda")


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    global flux_pipe, qwen_pipe

    if os.environ.get("LOAD_FLUX", "1") == "1":
        flux_pipe = _load_flux()

    if os.environ.get("LOAD_QWEN_IMAGE", "1") == "1":
        qwen_pipe = _load_qwen_image()

    yield

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


@app.post("/v1/images/generations")
async def generate(req: GenerationRequest):
    if flux_pipe is None:
        raise HTTPException(503, "FLUX pipeline not loaded (set LOAD_FLUX=1)")
    w, h = _parse_size(req.size)
    steps = req.num_inference_steps or (20 if req.quality == "hd" else 14)
    result = flux_pipe(
        prompt=req.prompt,
        width=w,
        height=h,
        num_images_per_prompt=req.n,
        num_inference_steps=steps,
        guidance_scale=3.5,
    )
    return {
        "created": int(time.time()),
        "data": [{"b64_json": _pil_to_b64(img)} for img in result.images],
    }


@app.post("/v1/images/edits")
async def edit(req: EditRequest):
    if qwen_pipe is None:
        raise HTTPException(503, "Qwen image pipeline not loaded (set LOAD_QWEN_IMAGE=1)")
    w, h = _parse_size(req.size)
    steps = req.num_inference_steps or 20
    result = qwen_pipe(
        prompt=req.prompt,
        image=_b64_to_pil(req.image),
        width=w,
        height=h,
        num_images_per_prompt=req.n,
        num_inference_steps=steps,
    )
    images = result.images if hasattr(result, "images") else result
    return {
        "created": int(time.time()),
        "data": [{"b64_json": _pil_to_b64(img)} for img in images],
    }


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "flux_loaded": flux_pipe is not None,
        "qwen_image_loaded": qwen_pipe is not None,
    }
