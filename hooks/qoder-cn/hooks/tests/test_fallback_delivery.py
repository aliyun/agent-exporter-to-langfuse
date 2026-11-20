"""R-1 observation 3: langstash-unavailable fallback delivery.

The real langstash-deliver chain runs (no deliver_trace spy): a local HTTP
endpoint plays both langstash (POST /ingest) and the Langfuse OTel endpoint
(POST /api/public/otel/v1/traces).

Cases:
  - langstash enabled but its tier unavailable (HTTP 500): the chain must fall
    through and the trace must still be delivered by the direct Langfuse push.
  - langstash disabled (LANGSTASH_ENABLED=false): no langstash POST at all,
    direct push delivers the trace.

Distinguishes violating implementations: a hook that drops the trace when
langstash is unavailable (no direct-push POST observed), or that never
attempts langstash while it is enabled.
"""

import base64
import json

import support

SESSION = "sess-fallback"

PUBLIC_KEY = "pk-lf-test"
SECRET_KEY = "sk-lf-test"


def _transcript_rows():
    return [
        support.user_row("fallback question", "2024-01-03T00:00:00Z"),
        support.assistant_row(
            [support.text_block("fallback answer")], "msg_fb_1", "2024-01-03T00:00:01Z"
        ),
    ]


def _prepare(hook_env):
    transcript = support.write_transcript(
        hook_env / ".qoder-cn" / "projects" / "proj-fb" / f"{SESSION}.jsonl",
        _transcript_rows(),
    )
    return support.stop_payload(SESSION, transcript)


def _direct_posts(server):
    return [r for r in server.requests if r["path"] == "/api/public/otel/v1/traces"]


def _ingest_posts(server):
    return [r for r in server.requests if r["path"] == "/ingest"]


def _assert_direct_push_delivered(server):
    direct = _direct_posts(server)
    assert direct, (
        "trace must be delivered by the Langfuse direct push "
        "(POST /api/public/otel/v1/traces) when langstash is unavailable; "
        f"recorded requests: {[r['path'] for r in server.requests]}"
    )
    post = direct[0]
    expected_auth = "Basic " + base64.b64encode(
        f"{PUBLIC_KEY}:{SECRET_KEY}".encode()
    ).decode()
    assert post["authorization"] == expected_auth, (
        "direct push must authenticate with the configured Langfuse basic credentials"
    )
    assert post["content_type"] == "application/json"
    otlp = json.loads(post["body"])
    root = support.root_span(otlp)
    assert support.attr_str(root, "session.id") == SESSION, (
        "the fallback-delivered trace must be the session's own trace"
    )
    assert support.trace_tags(otlp) == ["qoder-cn"], (
        "the fallback-delivered trace must carry the default qoder-cn tag"
    )
    return otlp


class TestLangstashUnavailableFallback:
    def test_langstash_tier_failing_falls_through_to_direct_push(
        self, hook_env, delivery_server, run_stop_hook, monkeypatch
    ):
        """langstash enabled but returning 500 -> direct push delivers."""
        monkeypatch.setenv("LANGSTASH_ENABLED", "true")
        monkeypatch.setenv("LANGSTASH_URL", f"http://127.0.0.1:{delivery_server.server_address[1]}")
        monkeypatch.setenv("LANGSTASH_TIMEOUT", "5")
        monkeypatch.setenv("LANGFUSE_BASE_URL", f"http://127.0.0.1:{delivery_server.server_address[1]}")
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", PUBLIC_KEY)
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", SECRET_KEY)
        delivery_server.ingest_status = 500  # langstash tier is unavailable

        payload = _prepare(hook_env)
        rc = run_stop_hook(payload)

        assert rc == 0, "hook must exit 0 even when the langstash tier fails"
        ingest = _ingest_posts(delivery_server)
        assert ingest, (
            "langstash must be attempted while LANGSTASH_ENABLED=true "
            "(the failing tier is what triggers the fallback)"
        )
        _assert_direct_push_delivered(delivery_server)

    def test_langstash_disabled_delivers_via_direct_push(
        self, hook_env, delivery_server, run_stop_hook, monkeypatch
    ):
        """LANGSTASH_ENABLED=false -> no langstash POST, direct push delivers."""
        monkeypatch.setenv("LANGSTASH_ENABLED", "false")
        monkeypatch.setenv("LANGFUSE_BASE_URL", f"http://127.0.0.1:{delivery_server.server_address[1]}")
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", PUBLIC_KEY)
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", SECRET_KEY)

        payload = _prepare(hook_env)
        rc = run_stop_hook(payload)

        assert rc == 0
        assert not _ingest_posts(delivery_server), (
            "no langstash POST may happen when LANGSTASH_ENABLED=false, got "
            f"{[r['path'] for r in _ingest_posts(delivery_server)]}"
        )
        _assert_direct_push_delivered(delivery_server)
