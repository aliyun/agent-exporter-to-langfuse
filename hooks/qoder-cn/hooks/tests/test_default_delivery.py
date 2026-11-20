"""R-1 observation 1: default Stop delivery.

A synthetic Stop payload (qoder-cn transcript jsonl + a temp SharedClientCache
SQLite DB under the sandbox ``~/.qoder-cn``) processed by ``hook.main()`` must
produce a trace containing turns / generations / tool calls, with the
assistant token usage and model name enriched from the qoder-cn DB.

Distinguishes violating implementations: no DB enrichment (model falls back to
"unknown", usage_details missing), missing tool spans, missing generations, a
crash anywhere in the pipeline (no payload reaches the deliver_trace boundary).
"""

import json

import support

TS_USER = "2024-01-01T00:00:00Z"
TS_ASSIST_1 = "2024-01-01T00:00:01Z"
TS_TOOL_RESULT = "2024-01-01T00:00:02Z"
TS_ASSIST_2 = "2024-01-01T00:00:03Z"

DB_MODEL_1 = "qoder-cn-model-alpha"
DB_MODEL_2 = "qoder-cn-model-beta"

USER_TEXT = "please inspect the log file"
ASSIST_1_TEXT = "I will read the log file."
ASSIST_2_TEXT = "The log shows one warning."
TOOL_RESULT_TEXT = "log line 1\nlog line 2"


def _transcript_rows():
    return [
        support.user_row(USER_TEXT, TS_USER),
        support.assistant_row(
            [
                support.text_block(ASSIST_1_TEXT),
                support.tool_use_block("tu_1", "read_file", {"path": "app.log"}),
            ],
            msg_id="msg_assist_1",
            ts=TS_ASSIST_1,
        ),
        support.tool_result_row("tu_1", TOOL_RESULT_TEXT, TS_TOOL_RESULT),
        support.assistant_row(
            [support.text_block(ASSIST_2_TEXT)],
            msg_id="msg_assist_2",
            ts=TS_ASSIST_2,
        ),
        # non-message row: tolerated, must not break turn assembly
        support.attachment_row("notes.txt", TS_USER),
    ]


def _db_rows(session_id):
    return [
        support.db_row(session_id, TS_ASSIST_1, 120, 45, DB_MODEL_1, cached_tokens=8),
        support.db_row(session_id, TS_ASSIST_2, 200, 60, DB_MODEL_2),
    ]


class TestDefaultStopDelivery:
    def test_trace_has_turn_generations_tools_and_db_enrichment(
        self, hook_env, delivered_traces, run_stop_hook
    ):
        session_id = "sess-default-delivery"
        transcript = support.write_transcript(
            hook_env / ".qoder-cn" / "projects" / "proj-a" / f"{session_id}.jsonl",
            _transcript_rows(),
        )
        support.create_chat_db(
            hook_env / ".qoder-cn" / "shared_client" / "cache" / "db" / "local.db",
            _db_rows(session_id),
        )

        rc = run_stop_hook(support.stop_payload(session_id, transcript))

        assert rc == 0, "hook must exit 0 after processing the Stop event"
        assert len(delivered_traces) == 1, (
            "a single-turn session must deliver exactly one trace payload at the "
            f"deliver_trace boundary, got {len(delivered_traces)}"
        )
        otlp = delivered_traces[0]

        # --- turn: one root span carrying the session input/output ---
        root = support.root_span(otlp)
        assert support.attr_str(root, "session.id") == session_id, (
            "trace must be attributed to the Stop payload's session_id"
        )
        root_input = json.loads(support.attr_str(root, "langfuse.observation.input"))
        assert root_input == {"role": "user", "content": USER_TEXT}, (
            "turn input must be the transcript's user message text"
        )
        root_output = json.loads(support.attr_str(root, "langfuse.observation.output"))
        assert root_output == {"role": "assistant", "content": ASSIST_2_TEXT}, (
            "turn output must be the final assistant message text"
        )

        # --- generations: one per assistant message, enriched from the DB ---
        gens = support.spans_of_type(otlp, "generation")
        assert len(gens) == 2, (
            f"expected one generation span per assistant message (2), got {len(gens)}"
        )
        for gen in gens:
            assert gen.get("parentSpanId") == root["spanId"], (
                "generation spans must nest under the turn's root span"
            )
        gen_by_model = {
            support.attr_str(g, "langfuse.observation.model.name"): g for g in gens
        }
        assert set(gen_by_model) == {DB_MODEL_1, DB_MODEL_2}, (
            "both generation models must come from the qoder-cn DB "
            f"(models observed: {sorted(m for m in gen_by_model if m)})"
        )
        usage_1 = json.loads(
            support.attr_str(gen_by_model[DB_MODEL_1], "langfuse.observation.usage_details")
        )
        assert usage_1 == {"input": 120, "output": 45, "cache_read_input_tokens": 8}, (
            "first generation usage must match the DB row's token_info"
        )
        usage_2 = json.loads(
            support.attr_str(gen_by_model[DB_MODEL_2], "langfuse.observation.usage_details")
        )
        assert usage_2 == {"input": 200, "output": 60}, (
            "second generation usage must match the DB row's token_info "
            "(no cached_tokens column value -> no cache key)"
        )

        # --- generation output carries the tool call ---
        gen_1_output = json.loads(
            support.attr_str(gen_by_model[DB_MODEL_1], "langfuse.observation.output")
        )
        assert gen_1_output["tool_calls"] == [
            {"id": "tu_1", "name": "read_file", "input": {"path": "app.log"}}
        ], "generation output must expose the assistant's tool call"

        # --- tool call span with its result ---
        tools = support.spans_of_type(otlp, "tool")
        assert len(tools) == 1, (
            f"expected exactly one tool span for the single tool_use, got {len(tools)}"
        )
        tool = tools[0]
        assert tool["name"] == "Tool: read_file", "tool span must be named after the tool"
        assert support.attr_str(tool, "langfuse.observation.metadata.tool_name") == "read_file"
        assert support.attr_str(tool, "langfuse.observation.output") == TOOL_RESULT_TEXT, (
            "tool span output must be the matched tool_result content"
        )

    def test_default_tag_is_qoder_cn(self, hook_env, delivered_traces, run_stop_hook):
        """LANGFUSE_TAGS unset: the trace must carry the default tag qoder-cn."""
        session_id = "sess-default-tag"
        transcript = support.write_transcript(
            hook_env / ".qoder-cn" / "projects" / "proj-a" / f"{session_id}.jsonl",
            [
                support.user_row("hello", "2024-01-01T00:00:00Z"),
                support.assistant_row(
                    [support.text_block("hi")], "msg_1", "2024-01-01T00:00:01Z"
                ),
            ],
        )

        rc = run_stop_hook(support.stop_payload(session_id, transcript))

        assert rc == 0
        assert len(delivered_traces) == 1, (
            "hook must deliver the trace payload at the deliver_trace boundary"
        )
        assert support.trace_tags(delivered_traces[0]) == ["qoder-cn"], (
            "default trace tag must be qoder-cn when LANGFUSE_TAGS is unset"
        )
