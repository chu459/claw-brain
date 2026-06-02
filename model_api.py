"""
Claw-brain 微调模型 API 服务
轻量级 FastAPI，OpenAI 兼容接口
加载本地合并模型 /root/claw_brain_merged_v3/
"""

import os
import sys
import time
import json
import asyncio
from typing import List, Dict, Optional
from contextlib import asynccontextmanager

os.environ["HF_HOME"] = "/root/hf_cache"

import torch
from transformers import AutoTokenizer, AutoModelForCausalLM, BitsAndBytesConfig
from fastapi import FastAPI
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel

# ============ 配置 ============
MODEL_PATH = "/root/claw_brain_merged_v3"
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
MAX_LENGTH = 4096

# 全局模型/分词器
tokenizer = None
model = None


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    model: str = "claw-brain"
    messages: List[ChatMessage]
    temperature: float = 0.7
    max_tokens: int = 2048
    stream: bool = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    global tokenizer, model
    print(f"[{time.strftime('%H:%M:%S')}] 正在加载模型: {MODEL_PATH}")
    print(f"[{time.strftime('%H:%M:%S')}] 设备: {DEVICE}")
    if DEVICE == "cuda":
        print(f"[{time.strftime('%H:%M:%S')}] GPU: {torch.cuda.get_device_name(0)}")

    tokenizer = AutoTokenizer.from_pretrained(MODEL_PATH, trust_remote_code=True)

    # 4-bit 量化加载，节省显存
    bnb_config = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_compute_dtype=torch.float16,
        bnb_4bit_use_double_quant=True,
        bnb_4bit_quant_type="nf4",
    )

    model = AutoModelForCausalLM.from_pretrained(
        MODEL_PATH,
        quantization_config=bnb_config,
        device_map="auto",
        trust_remote_code=True,
        torch_dtype=torch.float16,
    )

    print(f"[{time.strftime('%H:%M:%S')}] 模型加载完成!")
    yield
    print(f"[{time.strftime('%H:%M:%S')}] 服务关闭")


app = FastAPI(title="Claw-brain Model API", lifespan=lifespan)


def build_prompt(messages: List[Dict]) -> str:
    """构建 Qwen chat 格式 prompt"""
    text = ""
    for msg in messages:
        role = msg["role"]
        content = msg["content"]
        if role == "system":
            text += f"<|im_start|>system\n{content}<|im_end|>\n"
        elif role == "user":
            text += f"<|im_start|>user\n{content}<|im_end|>\n"
        elif role == "assistant":
            text += f"<|im_start|>assistant\n{content}<|im_end|>\n"
    text += "<|im_start|>assistant\n"
    return text


@app.get("/health")
def health():
    return {"status": "ok", "model_loaded": model is not None, "device": DEVICE}


@app.get("/v1/models")
def list_models():
    return {
        "object": "list",
        "data": [
            {"id": "claw-brain", "object": "model", "owned_by": "claw-brain"}
        ]
    }


@app.post("/v1/chat/completions")
def chat_completion(req: ChatRequest):
    if model is None:
        return JSONResponse({"error": "模型未加载"}, status_code=503)

    prompt = build_prompt([m.model_dump() for m in req.messages])
    inputs = tokenizer(prompt, return_tensors="pt").to(model.device)

    start_time = time.time()

    with torch.no_grad():
        outputs = model.generate(
            **inputs,
            max_new_tokens=req.max_tokens,
            temperature=req.temperature,
            do_sample=req.temperature > 0,
            top_p=0.9,
            pad_token_id=tokenizer.pad_token_id,
            eos_token_id=tokenizer.eos_token_id,
        )

    # 只取生成的部分
    generated_tokens = outputs[0][inputs.input_ids.shape[1]:]
    response_text = tokenizer.decode(generated_tokens, skip_special_tokens=True)

    prompt_tokens = inputs.input_ids.shape[1]
    completion_tokens = len(generated_tokens)

    return {
        "id": f"chatcmpl-{int(time.time())}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": req.model,
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": response_text,
            },
            "finish_reason": "stop"
        }],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }
    }


if __name__ == "__main__":
    import uvicorn
    print("启动 Claw-brain 模型 API...")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
