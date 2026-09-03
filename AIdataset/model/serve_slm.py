#!/usr/bin/env python3
"""Serve the fine-tuned filing model behind an OpenAI-compatible endpoint.

    pip install torch transformers accelerate      # separate venv from the bot
    python serve_slm.py --model ./slm_v1 --port 8001

    # then in the bot: Settings -> AI analysis
    #   AI model      = slm
    #   SLM endpoint  = http://127.0.0.1:8001/v1/chat/completions

Why this exists when `vllm serve` is one line: vLLM has no Windows build and
wants a GPU. This runs anywhere transformers runs, including a CPU laptop, so
anyone can reproduce the demo without renting hardware. If you HAVE a Linux
box with a GPU, use vLLM instead — it is 20-50x faster and speaks the same
protocol, so the bot cannot tell the difference.

Deliberately stdlib-only for the HTTP side: this process already carries torch,
and a second web framework to answer one route is not worth the install.

Implements exactly the slice the bot uses: POST /v1/chat/completions with
`messages`, `temperature`, `max_tokens`, `response_format`. Everything else in
the OpenAI spec is out of scope.
"""
from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = None
TOKENIZER = None
MODEL_NAME = "tradebot-slm-v1"
# ponytail: one global lock — a 1.5B on CPU is compute-bound, so concurrent
# generation is slower than serialised. Drop it if you move to a real GPU
# server (or just use vLLM, which batches properly).
LOCK = threading.Lock()


def load(path: str, device: str, dtype: str) -> None:
    global MODEL, TOKENIZER
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    torch_dtype = {"auto": "auto", "float32": torch.float32,
                   "bfloat16": torch.bfloat16, "float16": torch.float16}[dtype]
    print(f"loading {path} onto {device} ({dtype}) ...", flush=True)
    t0 = time.perf_counter()
    TOKENIZER = AutoTokenizer.from_pretrained(path)
    MODEL = AutoModelForCausalLM.from_pretrained(path, dtype=torch_dtype)
    MODEL.to(device)
    MODEL.eval()
    print(f"loaded in {time.perf_counter() - t0:.1f}s", flush=True)


def generate(messages: list[dict], temperature: float, max_tokens: int) -> tuple[str, int, int]:
    import torch

    prompt = TOKENIZER.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=True
    )
    inputs = TOKENIZER(prompt, return_tensors="pt").to(MODEL.device)
    prompt_tokens = int(inputs["input_ids"].shape[-1])
    with LOCK, torch.no_grad():
        out = MODEL.generate(
            **inputs,
            max_new_tokens=max_tokens,
            # temperature 0 means "be deterministic", which is what the bot
            # wants from a classifier. HF errors on temperature=0, so switch
            # to greedy instead of clamping to a tiny non-zero value.
            do_sample=temperature > 0,
            temperature=temperature if temperature > 0 else None,
            top_p=0.8 if temperature > 0 else None,
            pad_token_id=TOKENIZER.pad_token_id or TOKENIZER.eos_token_id,
        )
    completion = out[0][prompt_tokens:]
    text = TOKENIZER.decode(completion, skip_special_tokens=True)
    return text, prompt_tokens, int(completion.shape[-1])


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._send(200, {"status": "ok", "model": MODEL_NAME})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._send(404, {"error": "not found"})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
            messages = req.get("messages") or []
            if not messages:
                self._send(400, {"error": "messages is required"})
                return
            t0 = time.perf_counter()
            text, p_tok, c_tok = generate(
                messages,
                float(req.get("temperature", 0.2)),
                int(req.get("max_tokens", 400)),
            )
            took = time.perf_counter() - t0
            print(f"{c_tok} tok in {took:.1f}s ({c_tok / max(took, 1e-9):.1f} tok/s)", flush=True)
            self._send(200, {
                "id": "chatcmpl-slm",
                "object": "chat.completion",
                "model": MODEL_NAME,
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": text}}],
                "usage": {"prompt_tokens": p_tok, "completion_tokens": c_tok,
                          "total_tokens": p_tok + c_tok},
            })
        except Exception as e:  # noqa: BLE001
            # 500 is retryable on the bot's side; a crash here must not look
            # like a malformed request that will never succeed.
            self._send(500, {"error": repr(e)})

    def log_message(self, *_a) -> None:
        pass  # we print our own one-liner per generation


def main() -> None:
    global MODEL_NAME
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="./slm_v1")
    ap.add_argument("--port", type=int, default=8001)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--device", default="cpu", help="cpu | cuda")
    ap.add_argument("--dtype", default="auto", choices=["auto", "float32", "bfloat16", "float16"])
    ap.add_argument("--served-model-name", default="tradebot-slm-v1")
    args = ap.parse_args()

    MODEL_NAME = args.served_model_name
    load(args.model, args.device, args.dtype)
    print(f"POST http://{args.host}:{args.port}/v1/chat/completions", flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
