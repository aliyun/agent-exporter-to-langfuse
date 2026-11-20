"""R-1 observation 5: DB missing degradation.

With no SharedClientCache DB anywhere in the sandbox home, the Stop delivery
must still complete normally — the trace (turn + generation) is produced —
only the token/model enrichment is missing (model falls back to "unknown" and
no usage_details attribute).

Distinguishes violating implementations: crashing on a missing DB (no payload
reaches the deliver_trace boundary) or a missing trace.
"""

import support

SESSION = "sess-db-missing"

TS_USER = "2024-01-05T00:00:00Z"
TS_ASSIST = "2024-01-05T00:00:01Z"

ASSISTANT_TEXT = "answer without usage data"


class TestDbMissing:
    def test_trace_delivered_without_token_model_when_db_absent(
        self, hook_env, delivered_traces, run_stop_hook
    ):
        transcript = support.write_transcript(
            hook_env / ".qoder-cn" / "projects" / "proj-nodb" / f"{SESSION}.jsonl",
            [
                support.user_row("question without db", TS_USER),
                support.assistant_row(
                    [support.text_block(ASSISTANT_TEXT)], "msg_nodb_1", TS_ASSIST
                ),
            ],
        )
        # no SharedClientCache DB fixture created anywhere under hook_env

        rc = run_stop_hook(support.stop_payload(SESSION, transcript))

        assert rc == 0, "hook must exit 0 when the DB is absent"
        assert len(delivered_traces) == 1, (
            "the trace must still be delivered at the deliver_trace boundary "
            "when the DB is absent; a crash would deliver nothing"
        )
        otlp = delivered_traces[0]

        root = support.root_span(otlp)
        assert support.attr_str(root, "session.id") == SESSION, (
            "the delivered trace must still be attributed to the session"
        )

        gens = support.spans_of_type(otlp, "generation")
        assert len(gens) == 1, (
            "the generation span must still be produced without DB enrichment"
        )
        gen = gens[0]
        model = support.attr_str(gen, "langfuse.observation.model.name")
        assert model == "unknown", (
            "with no DB the model must fall back to 'unknown'; observed: "
            f"{model!r}"
        )
        assert support.get_attr(gen, "langfuse.observation.usage_details") is None, (
            "with no DB there must be no usage_details attribute"
        )
