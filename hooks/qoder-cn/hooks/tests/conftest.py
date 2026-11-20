"""Shared fixtures for the qoder-cn hook behavior-level test suite.

Path injection follows the claude-code precedent (hooks/claude-code/hooks/tests/
conftest.py): the hook directory and the langstash-deliver python package are
both placed on ``sys.path`` so ``import langfuse_hook`` resolves.

``HOME`` is redirected to a sandbox directory BEFORE ``langfuse_hook`` is
imported so that the module-level ``Path.home()``-derived constants (STATE_DIR
/ LOG_FILE / STATE_FILE / LOCK_FILE, and langstash_deliver's INSTALL_DIR)
never point at the real user home while the suite runs.

Each test redirects ``HOME`` again (per-test sandbox home) via the
``hook_env`` fixture so that ``_find_qoder_db()`` — which resolves
``Path.home()`` at call time — sees only the DB fixtures that test created,
and the hook's incremental state lands at ``<sandbox>/.qoder-cn/state/``.

R-1 observation points covered by this suite (one module each):
  1 default delivery with DB token/model      -> test_default_delivery.py
  2 LANGFUSE_TAGS semantics                   -> test_langfuse_tags.py
  3 langstash-unavailable fallback delivery   -> test_fallback_delivery.py
  4 cross-DB discrimination                   -> test_db_discrimination.py
  5 DB missing degradation                    -> test_db_missing.py
  6 incremental delivery + state              -> test_incremental_delivery.py
"""

import atexit
import io
import json
import os
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

# --- Sandbox HOME (must precede the langfuse_hook import) -------------------
_SANDBOX_HOME = Path(tempfile.mkdtemp(prefix="qoder-cn-hook-tests-home-"))
os.environ["HOME"] = str(_SANDBOX_HOME)
atexit.register(shutil.rmtree, str(_SANDBOX_HOME), ignore_errors=True)

# --- sys.path injection (claude-code conftest precedent) --------------------
_HOOKS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HOOKS_DIR))

_LANGSTASH_DELIVER_DIR = _HOOKS_DIR.parent.parent / "langstash-deliver" / "python"
if _LANGSTASH_DELIVER_DIR.exists():
    sys.path.insert(0, str(_LANGSTASH_DELIVER_DIR))

import langfuse_hook as hook  # noqa: E402  (import after HOME/sys.path setup)


# Configuration the hook reads from the environment. Cleared per test so a
# developer shell exporting LANGFUSE_*/LANGSTASH_* cannot skew results.
_HOOK_ENV_VARS = (
    "LANGFUSE_TAGS",
    "LANGFUSE_USER_ID",
    "LANGFUSE_DEBUG",
    "LANGFUSE_MAX_CHARS",
    "LANGSTASH_ENABLED",
    "LANGSTASH_URL",
    "LANGSTASH_TIMEOUT",
    "LANGFUSE_BASE_URL",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
)


@pytest.fixture
def hook_env(tmp_path, monkeypatch):
    """Per-test sandbox home with redirected hook state and clean config.

    The module-level state constants (STATE_DIR / LOG_FILE / STATE_FILE /
    LOCK_FILE) are module globals read at call time by the hook's state and
    locking helpers, so redirecting them here points the incremental state at
    ``<sandbox>/.qoder-cn/state/`` — the production layout relative to the
    sandbox home — while keeping every test isolated.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    state_dir = home / ".qoder-cn" / "state"
    monkeypatch.setattr(hook, "STATE_DIR", state_dir)
    monkeypatch.setattr(hook, "LOG_FILE", state_dir / "langfuse_hook.log")
    monkeypatch.setattr(hook, "STATE_FILE", state_dir / "langfuse_state.json")
    monkeypatch.setattr(hook, "LOCK_FILE", state_dir / "langfuse_state.lock")
    for var in _HOOK_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    return home


@pytest.fixture
def delivered_traces(monkeypatch):
    """Record every OTLP payload crossing the deliver_trace call boundary.

    The spy replaces ``langfuse_hook.deliver_trace`` (the module-global name
    ``main()`` resolves at call time) so the payload->turn->OTLP construction
    path runs for real while the actual network delivery is captured.
    """
    calls = []

    def _record(otlp_json):
        calls.append(otlp_json)
        return True

    monkeypatch.setattr(hook, "deliver_trace", _record)
    return calls


@pytest.fixture
def run_stop_hook(monkeypatch):
    """Run ``hook.main()`` with a JSON payload on stdin (qoder-cn Stop event)."""

    def _run(payload: dict) -> int:
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        return hook.main()

    return _run


class _RecordingHandler(BaseHTTPRequestHandler):
    """Records every POST; /ingest status is controlled by the test."""

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or "0")
        body = self.rfile.read(length) if length else b""
        self.server.requests.append(
            {
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "content_type": self.headers.get("Content-Type"),
                "body": body,
            }
        )
        status = self.server.ingest_status if self.path == "/ingest" else 200
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format, *args):  # keep pytest output clean
        pass


@pytest.fixture
def delivery_server():
    """Local HTTP endpoint playing both langstash (POST /ingest) and the
    Langfuse OTel endpoint (POST /api/public/otel/v1/traces).

    Lets the real langstash-deliver chain run its fallback semantics without
    touching any real service. ``ingest_status`` is settable per test (e.g.
    500 to make the langstash tier unavailable).
    """
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RecordingHandler)
    server.requests = []
    server.ingest_status = 200
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
