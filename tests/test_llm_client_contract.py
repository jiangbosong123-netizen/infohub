import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

from app import config
from app.ai import pipeline

REPLY = [{"id": 1, "title_zh": "标题", "tmt": True, "score": 70, "summary_zh": "摘要", "reason": "理由",
          "event_type": None, "ai_cat": "product"}]


class _Provider(BaseHTTPRequestHandler):
    """An OpenAI-compatible endpoint on localhost that records what the client sends."""

    requests: list = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.requests.append({"path": self.path, "authorization": self.headers.get("Authorization"),
                              "body": body})
        data = json.dumps({
            "id": "chatcmpl-test", "object": "chat.completion", "created": 0, "model": body["model"],
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": json.dumps(REPLY, ensure_ascii=False)}}],
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class LlmClientContractTests(unittest.TestCase):
    """The pinned openai client sends what the GLM endpoint expects; other tests mock it away."""

    def test_curation_request_and_reply(self):
        _Provider.requests = []
        server = HTTPServer(("127.0.0.1", 0), _Provider)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with patch.multiple(config, LLM_BASE_URL=f"http://127.0.0.1:{server.server_port}/api/paas/v4",
                            LLM_API_KEY="test-key", LLM_MODEL="glm-4.6"):
            result = pipeline._call_llm([{"id": 1, "channel": "ai", "title": "OpenAI launches a model",
                                          "excerpt": ""}])
        self.assertEqual(result, REPLY)
        [request] = _Provider.requests
        self.assertEqual(request["path"], "/api/paas/v4/chat/completions")
        self.assertEqual(request["authorization"], "Bearer test-key")
        body = request["body"]
        self.assertEqual((body["model"], body["temperature"]), ("glm-4.6", 0.2))
        # extra_body reaches the provider as a top-level field (GLM's switch for deep thinking).
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertEqual([message["role"] for message in body["messages"]], ["system", "user"])
        self.assertEqual(json.loads(body["messages"][1]["content"])[0]["id"], 1)


if __name__ == "__main__":
    unittest.main()
