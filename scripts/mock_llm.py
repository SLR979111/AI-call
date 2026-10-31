#!/usr/bin/env python3
"""Tiny OpenAI-compatible mock LLM for local end-to-end tests.

Usage:
  python scripts/mock_llm.py --port 9000
  LLM_PRIMARY_BASE_URL=http://127.0.0.1:9000/v1 \
  LLM_PRIMARY_API_KEY=mock LLM_PRIMARY_MODEL=mock \
  AGENT_PROVIDER=llm uvicorn app.main:app --port 8082
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def decide(user_text: str) -> dict:
    text = user_text
    if any(word in text for word in ("订单", "改约", "改到", "改吧", "配送")):
        return {
            "intent": "reschedule",
            "reply": "可以的，我先帮您看一下订单。",
            "slots": {"order_id": "A12345678"},
            "tool": "order.query",
            "tool_args": {"order_id": "A12345678"},
            "outcome": "rescheduled",
        }
    if any(word in text for word in ("退款", "权益", "多久", "政策", "录音")):
        return {
            "intent": "faq",
            "reply": "我帮您查一下相关规定。",
            "slots": {},
            "outcome": "answered",
        }
    if any(word in text for word in ("人工", "投诉")):
        return {"intent": "transfer", "reply": "好的，为您转接人工。", "outcome": "transferred"}
    return {"intent": "other", "reply": "好的，我明白了。", "outcome": "completed"}


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") == "/health":
            self._send(200, {"status": "ok"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            request = json.loads(raw.decode("utf-8"))
        except Exception:
            self._send(400, {"error": "invalid json"})
            return
        messages = request.get("messages") or []
        user_text = ""
        for message in reversed(messages):
            if message.get("role") == "user":
                user_text = str(message.get("content", ""))
                break
        decision = decide(user_text)
        self._send(200, {
            "id": "mock-chatcmpl-1",
            "object": "chat.completion",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": json.dumps(decision, ensure_ascii=False)},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 40, "completion_tokens": 20, "total_tokens": 60},
        })

    def log_message(self, fmt: str, *args) -> None:
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"mock LLM listening on http://{args.host}:{args.port}/v1")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
