import base64
import io
import os
from contextlib import asynccontextmanager
from typing import Optional

import torch
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

model = None
processor = None

_OCR_SYSTEM = (
    "You are an OCR engine. Extract all text from the image exactly as it appears, "
    "preserving layout, line breaks, and punctuation. "
    "Output only the extracted text — no commentary, no explanations, no markdown."
)


def _load_model():
    from transformers import AutoProcessor, Qwen2_5_VLForConditionalGeneration

    model_id = os.environ.get("OCR_MODEL_ID", "Qwen/Qwen2.5-VL-7B-Instruct")
    m = Qwen2_5_VLForConditionalGeneration.from_pretrained(
        model_id,
        torch_dtype=torch.bfloat16,
        device_map="auto",
    )
    p = AutoProcessor.from_pretrained(model_id)
    return m, p


@asynccontextmanager
async def lifespan(app: FastAPI):
    global model, processor
    model, processor = _load_model()
    yield
    del model, processor
    torch.cuda.empty_cache()


app = FastAPI(title="ocr-api", lifespan=lifespan)


class OCRRequest(BaseModel):
    image: str                         # base64-encoded image OR https:// URL
    language_hint: Optional[str] = None
    max_new_tokens: int = 2048
    prompt: Optional[str] = None       # override the default OCR user prompt


def _image_content(image_str: str) -> dict:
    if image_str.startswith("http://") or image_str.startswith("https://"):
        return {"type": "image", "image": image_str}
    data = base64.b64decode(image_str)
    mime = "image/png" if data[:4] == b"\x89PNG" else "image/jpeg"
    return {"type": "image", "image": f"data:{mime};base64,{base64.b64encode(data).decode()}"}


@app.post("/v1/ocr")
async def ocr(req: OCRRequest):
    if model is None or processor is None:
        raise HTTPException(503, "model not loaded")

    user_text = req.prompt or "Extract all text from this image."
    if req.language_hint:
        user_text += f" The text language is {req.language_hint}."

    messages = [
        {"role": "system", "content": _OCR_SYSTEM},
        {
            "role": "user",
            "content": [_image_content(req.image), {"type": "text", "text": user_text}],
        },
    ]

    from qwen_vl_utils import process_vision_info

    text = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    image_inputs, video_inputs = process_vision_info(messages)
    inputs = processor(
        text=[text],
        images=image_inputs,
        videos=video_inputs,
        padding=True,
        return_tensors="pt",
    ).to("cuda")

    with torch.no_grad():
        ids = model.generate(**inputs, max_new_tokens=req.max_new_tokens)

    trimmed = [out[len(inp):] for inp, out in zip(inputs.input_ids, ids)]
    text_out = processor.batch_decode(trimmed, skip_special_tokens=True, clean_up_tokenization_spaces=False)

    return {
        "text": text_out[0].strip(),
        "model": os.environ.get("OCR_MODEL_ID", "Qwen/Qwen2.5-VL-7B-Instruct"),
    }


@app.get("/health")
async def health():
    return {"status": "ok", "model_loaded": model is not None}
