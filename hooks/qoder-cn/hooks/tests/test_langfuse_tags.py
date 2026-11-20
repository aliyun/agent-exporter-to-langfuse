"""R-1 observation 2: LANGFUSE_TAGS semantics (qoder-identical).

qoder's semantics: ``tags_raw = _opt("LANGFUSE_TAGS") or "qoder-cn"`` — the
env value is used verbatim (comma-separated) and the default only applies when
the variable is unset/empty. In a real installed environment the installer
composes the env value as ``qoder-cn,<user tags>`` (see hooks/qoder/install.sh
FINAL_TAGS), so the observed tag set is the default tag plus user tags.

Distinguishes violating implementations: a hardcoded tag that ignores user
tags (user tags missing), or an implementation that silently re-appends the
default when the env value already replaces it (differs from qoder).
"""

import pytest

import support

TS_USER = "2024-01-02T00:00:00Z"
TS_ASSIST = "2024-01-02T00:00:01Z"


def _run_session(hook_env, delivered_traces, run_stop_hook, session_id):
    transcript = support.write_transcript(
        hook_env / ".qoder-cn" / "projects" / "proj-tags" / f"{session_id}.jsonl",
        [
            support.user_row("tag question", TS_USER),
            support.assistant_row(
                [support.text_block("tag answer")], "msg_tags_1", TS_ASSIST
            ),
        ],
    )
    rc = run_stop_hook(support.stop_payload(session_id, transcript))
    assert rc == 0, "hook must exit 0 after processing the Stop event"
    assert len(delivered_traces) == 1, (
        f"expected exactly one delivered trace, got {len(delivered_traces)}"
    )
    return delivered_traces[0]


class TestLangfuseTags:
    @pytest.mark.parametrize(
        ("tags_value", "expected_tags"),
        [
            # installer-composed value: default tag + user tags coexist
            ("qoder-cn,team-a,exp-1", ["qoder-cn", "team-a", "exp-1"]),
            # user value without the default: qoder's `or` semantics replace it
            ("custom-only", ["custom-only"]),
            # whitespace is trimmed per tag
            ("qoder-cn, spaced ", ["qoder-cn", "spaced"]),
        ],
        ids=["default-plus-user-tags", "env-replaces-default", "tag-whitespace-trimmed"],
    )
    def test_langfuse_tags_semantics(
        self, hook_env, delivered_traces, run_stop_hook, monkeypatch, tags_value, expected_tags
    ):
        monkeypatch.setenv("LANGFUSE_TAGS", tags_value)
        session_id = "sess-tags-" + tags_value.replace(",", "-").replace(" ", "")
        otlp = _run_session(hook_env, delivered_traces, run_stop_hook, session_id)

        assert support.trace_tags(otlp) == expected_tags, (
            f"LANGFUSE_TAGS={tags_value!r} must yield tags {expected_tags} "
            f"(qoder semantics: tags_raw = LANGFUSE_TAGS or default)"
        )

    def test_unset_langfuse_tags_falls_back_to_default(
        self, hook_env, delivered_traces, run_stop_hook
    ):
        otlp = _run_session(
            hook_env, delivered_traces, run_stop_hook, "sess-tags-unset"
        )
        assert support.trace_tags(otlp) == ["qoder-cn"], (
            "unset LANGFUSE_TAGS must fall back to the default tag qoder-cn"
        )

    def test_empty_langfuse_tags_falls_back_to_default(
        self, hook_env, delivered_traces, run_stop_hook, monkeypatch
    ):
        monkeypatch.setenv("LANGFUSE_TAGS", "")
        otlp = _run_session(
            hook_env, delivered_traces, run_stop_hook, "sess-tags-empty"
        )
        assert support.trace_tags(otlp) == ["qoder-cn"], (
            "empty LANGFUSE_TAGS must fall back to the default tag qoder-cn "
            "(_opt returns '' for empty values)"
        )
