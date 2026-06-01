FROM nvcr.io/nvidia/pytorch:25.08-py3
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# The base image ships torchao 0.12 (git), but peft>=0.19 raises ImportError on
# torchao <0.16 while injecting LoRA layers. We run FLUX/Qwen in bf16 and never
# use torchao, so remove it → peft takes its no-torchao path and LoRA loads.
RUN pip uninstall -y torchao || true
COPY main.py .
EXPOSE 8002
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8002"]
