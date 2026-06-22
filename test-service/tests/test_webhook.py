import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

import pytest

from src.config import WebhookConfig
from src.webhook import WebhookNotifier


class _MockHandler(BaseHTTPRequestHandler):
    received: list = []
    status_code: int = 200

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}
        _MockHandler.received.append(body)
        self.send_response(_MockHandler.status_code)
        self.end_headers()

    def log_message(self, format, *args):
        pass


@pytest.fixture
def mock_server():
    _MockHandler.received = []
    _MockHandler.status_code = 200
    server = HTTPServer(("127.0.0.1", 0), _MockHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}/webhook"
    server.shutdown()


def test_successful_callback(mock_server):
    config = WebhookConfig(retry_count=1, retry_delays=[0], timeout_seconds=5)
    notifier = WebhookNotifier(config)
    notifier.notify(mock_server, {"event": "e2e.completed", "status": "success"})

    import time
    time.sleep(0.5)

    assert len(_MockHandler.received) == 1
    assert _MockHandler.received[0]["status"] == "success"


def test_no_callback_url():
    config = WebhookConfig()
    notifier = WebhookNotifier(config)
    notifier.notify("", {"event": "e2e.completed"})


def test_retry_on_failure(mock_server):
    _MockHandler.status_code = 500
    config = WebhookConfig(retry_count=2, retry_delays=[0, 0], timeout_seconds=5)
    notifier = WebhookNotifier(config)
    notifier.notify(mock_server, {"event": "e2e.completed"})

    import time
    time.sleep(1)

    assert len(_MockHandler.received) == 3  # 1 initial + 2 retries
