from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import torch
from transformers import AutoTokenizer, BitsAndBytesConfig, Qwen3_5ForCausalLM

from tool_protocol import ToolProtocolError, parse_qwen_response, to_openai_message


MODEL_PATH = os.environ["PRO_RUN_MODEL_PATH"]
MODEL_ID = os.getenv("PRO_RUN_MODEL_ID", "qwen3.5-4b-local")
HOST = os.getenv("PRO_RUN_MODEL_HOST", "127.0.0.1")
PORT = int(os.getenv("PRO_RUN_MODEL_PORT", "18081"))

tokenizer = AutoTokenizer.from_pretrained(MODEL_PATH, local_files_only=True)
quantization = BitsAndBytesConfig(
    load_in_4bit=True,
    bnb_4bit_quant_type="nf4",
    bnb_4bit_use_double_quant=True,
    bnb_4bit_compute_dtype=torch.bfloat16,
)
model = Qwen3_5ForCausalLM.from_pretrained(
    MODEL_PATH,
    local_files_only=True,
    quantization_config=quantization,
    dtype=torch.bfloat16,
    device_map={"": 0},
)
model.eval()


def generate(
    messages: list[dict[str, object]],
    tools: list[dict[str, object]],
) -> str:
    template_kwargs: dict[str, object] = {}
    if tools:
        template_kwargs["tools"] = tools
    try:
        prompt = tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
            enable_thinking=False,
            **template_kwargs,
        )
    except TypeError:
        prompt = tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
            **template_kwargs,
        )
    inputs = tokenizer(prompt, return_tensors="pt").to(model.device)
    with torch.inference_mode():
        output = model.generate(**inputs, max_new_tokens=256, do_sample=False)
    return tokenizer.decode(
        output[0][inputs["input_ids"].shape[1] :],
        skip_special_tokens=True,
    ).strip()


class Handler(BaseHTTPRequestHandler):
    def send_json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/v1/models":
            self.send_json(
                200,
                {"object": "list", "data": [{"id": MODEL_ID, "object": "model"}]},
            )
            return
        self.send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/v1/chat/completions":
            self.send_json(404, {"error": "not found"})
            return
        try:
            request = json.loads(
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
            )
            messages = request.get("messages")
            tools = request.get("tools") or []
            if not isinstance(messages, list) or not all(
                isinstance(item, dict) for item in messages
            ):
                raise ValueError("messages must be an array of objects")
            if not isinstance(tools, list) or not all(
                isinstance(item, dict) for item in tools
            ):
                raise ValueError("tools must be an array of objects")

            raw = generate(messages, tools)
            parsed = parse_qwen_response(raw, tools=tools, messages=messages)
            message, finish_reason = to_openai_message(parsed)
            self.send_json(
                200,
                {
                    "id": "chatcmpl-local",
                    "object": "chat.completion",
                    "model": MODEL_ID,
                    "choices": [
                        {
                            "index": 0,
                            "message": message,
                            "finish_reason": finish_reason,
                        }
                    ],
                },
            )
        except ToolProtocolError as exc:
            self.send_json(422, {"error": f"ToolProtocolError: {exc}"})
        except Exception as exc:
            self.send_json(500, {"error": f"{type(exc).__name__}: {exc}"})

    def log_message(self, fmt: str, *args: object) -> None:
        print("HTTP|" + (fmt % args), flush=True)


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"READY|http://{HOST}:{PORT}/v1", flush=True)
    server.serve_forever()