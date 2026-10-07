from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from model_identity import model_provenance


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

if os.getenv("PRE_ACTIVE_QWEN_CONSOLE") != "1":
    sys.stdout = open(LOG_DIR / "qwen-http.out.log", "a", encoding="utf-8", buffering=1)
    sys.stderr = open(LOG_DIR / "qwen-http.err.log", "a", encoding="utf-8", buffering=1)

MODEL_PATH_FILE = Path(
    os.getenv(
        "PRE_ACTIVE_MODEL_PATH_FILE",
        str(RUNTIME_DIR / "model-path.txt"),
    )
)
MODEL_PATH = os.getenv("PRE_ACTIVE_MODEL_PATH")
if not MODEL_PATH:
    if not MODEL_PATH_FILE.is_file():
        raise RuntimeError(f"model path binding missing: {MODEL_PATH_FILE}")
    MODEL_PATH = MODEL_PATH_FILE.read_text(encoding="utf-8").strip()
if not MODEL_PATH:
    raise RuntimeError("model path binding is empty")
MODEL_PROVENANCE = model_provenance(MODEL_PATH)

MODEL_ID = os.getenv("PRE_ACTIVE_MODEL_ID", "qwen3.5-4b-local")
HOST = os.getenv("PRE_ACTIVE_MODEL_HOST", "127.0.0.1")
PORT = int(os.getenv("PRE_ACTIVE_MODEL_PORT", "18081"))
MIN_FREE_VRAM_MIB = int(os.getenv("PRE_ACTIVE_MIN_FREE_VRAM_MIB", "3400"))
NVIDIA_SMI = os.getenv(
    "PRE_ACTIVE_NVIDIA_SMI",
    r"C:\Windows\System32\nvidia-smi.exe",
)

MODEL_SITE_PACKAGES = Path(
    os.getenv(
        "PRE_ACTIVE_MODEL_SITE_PACKAGES",
        str(ROOT / "model-env" / "Lib" / "site-packages"),
    )
)
if not MODEL_SITE_PACKAGES.is_dir():
    raise RuntimeError(f"model site-packages missing: {MODEL_SITE_PACKAGES}")
sys.path.insert(0, str(MODEL_SITE_PACKAGES))


def _port_is_busy() -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    try:
        return sock.connect_ex((HOST, PORT)) == 0
    finally:
        sock.close()


def _free_vram_mib() -> int:
    result = subprocess.run(
        [
            NVIDIA_SMI,
            "--query-gpu=memory.free",
            "--format=csv,noheader,nounits",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"nvidia-smi failed with exit {result.returncode}: {result.stderr.strip()}"
        )
    first = result.stdout.splitlines()[0].strip()
    return int("".join(ch for ch in first if ch.isdigit()) or "0")


while _port_is_busy():
    print(f"PORT_ALREADY_LISTENING|host={HOST}|port={PORT}", flush=True)
    time.sleep(15)

while True:
    free_mib = _free_vram_mib()
    if free_mib >= MIN_FREE_VRAM_MIB:
        print(
            f"GPU_READY|free_mib={free_mib}|required_mib={MIN_FREE_VRAM_MIB}",
            flush=True,
        )
        break
    print(
        f"WAIT_GPU|free_mib={free_mib}|required_mib={MIN_FREE_VRAM_MIB}",
        flush=True,
    )
    time.sleep(15)


import torch  # noqa: E402
from transformers import (  # noqa: E402
    AutoTokenizer,
    BitsAndBytesConfig,
    Qwen3_5ForCausalLM,
)

from tool_protocol import (  # noqa: E402
    ToolProtocolError,
    parse_qwen_response,
    to_openai_message,
)


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
            model_record: dict[str, object] = {
                "id": MODEL_ID,
                "object": "model",
            }
            if MODEL_PROVENANCE is not None:
                model_record["provenance"] = MODEL_PROVENANCE
            self.send_json(
                200,
                {"object": "list", "data": [model_record]},
            )
            return
        self.send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/v1/chat/completions":
            self.send_json(404, {"error": "not found"})
            return
        try:
            payload = json.loads(
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
            )
            messages = payload.get("messages")
            tools = payload.get("tools") or []
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
