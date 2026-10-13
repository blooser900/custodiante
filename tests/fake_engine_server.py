"""A tiny System One server for tests: answers every question, after a delay.

Speaks real HTTP, so tests exercise the actual client (httpx, headers,
timeouts) rather than a mock of it.
"""

import json
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def answer_for(spec: dict) -> dict:
    kind = spec["type"]
    if kind == "noul":
        return {"type": "noul", "noul": 0.5}
    if kind == "choice":
        options = list(spec["criteria"])
        probabilities = {o: round(1 / len(options), 4) for o in options}
        return {"type": "choice", "choice": options[0], "confidence": 0.0, "probabilities": probabilities}
    levels = len(spec["criteria"])
    return {
        "type": "score", "score": 1.0, "confidence": 0.5,
        "probabilities": {str(i): round(1 / levels, 4) for i in range(levels)},
        "legend": {str(i): label for i, label in enumerate(spec["criteria"])},
    }


@contextmanager
def fake_engine(delay_seconds: float = 0.0):
    """Serve on a free local port; yields (url, received_payloads)."""
    received: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 -- http.server naming
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append(payload)
            time.sleep(delay_seconds)  # a slow model, e.g. on a CPU
            body = json.dumps({
                "model": "fake-1", "latency_ms": delay_seconds * 1000,
                "answers": {qid: answer_for(spec) for qid, spec in payload["questions"].items()},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # keep test output clean
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1/systemone", received
    finally:
        server.shutdown()
        server.server_close()
