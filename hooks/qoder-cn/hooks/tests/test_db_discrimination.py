"""R-1 observation 4: cross-DB discrimination (qoder-cn vs international Qoder).

On the same machine both the international Qoder SharedClientCache DB
(``~/Library/Application Support/Qoder/SharedClientCache/cache/db/local.db`` /
``~/.qoder/shared_client/cache/db/local.db``) and the qoder-cn DB
(``~/.qoder-cn/shared_client/cache/db/local.db`` / qoder-cn macOS App Support
``com.qodercn.app.stable``) may exist with completely independent session data.

The hook's DB candidates must be qoder-cn only, so the trace's token/model
enrichment must always come from the qoder-cn DB — never from an
international path.

Distinguishes violating implementations: any candidate list containing an
international path (wrong model/token values observed, or enrichment present
when only an international DB exists).
"""

import json
import sys

import pytest

import support

SESSION = "sess-cross-db"

TS_USER = "2024-01-04T00:00:00Z"
TS_ASSIST = "2024-01-04T00:00:01Z"

INTL_APP_SUPPORT_MODEL = "intl-appsupport-model"
INTL_DOT_QODER_MODEL = "intl-dotqoder-model"
QODER_CN_LOCAL_MODEL = "qoder-cn-local-model"
QODER_CN_APP_SUPPORT_MODEL = "qoder-cn-appsupport-model"


def _transcript():
    return [
        support.user_row("which database am I reading", TS_USER),
        support.assistant_row(
            [support.text_block("the qoder-cn one")], "msg_xdb_1", TS_ASSIST
        ),
    ]


def _create_intl_app_support_db(home):
    return support.create_chat_db(
        home / "Library" / "Application Support" / "Qoder" / "SharedClientCache" / "cache" / "db" / "local.db",
        [support.db_row(SESSION, TS_ASSIST, 999, 888, INTL_APP_SUPPORT_MODEL)],
    )


def _create_intl_dot_qoder_db(home):
    return support.create_chat_db(
        home / ".qoder" / "shared_client" / "cache" / "db" / "local.db",
        [support.db_row(SESSION, TS_ASSIST, 777, 666, INTL_DOT_QODER_MODEL)],
    )


def _create_qoder_cn_db(home):
    return support.create_chat_db(
        home / ".qoder-cn" / "shared_client" / "cache" / "db" / "local.db",
        [support.db_row(SESSION, TS_ASSIST, 111, 222, QODER_CN_LOCAL_MODEL)],
    )


def _create_qoder_cn_app_support_db(home):
    return support.create_chat_db(
        home / "Library" / "Application Support" / "com.qodercn.app.stable" / "SharedClientCache" / "cache" / "db" / "local.db",
        [support.db_row(SESSION, TS_ASSIST, 333, 444, QODER_CN_APP_SUPPORT_MODEL)],
    )


def _generation(delivered_traces):
    assert len(delivered_traces) == 1, (
        f"expected exactly one delivered trace, got {len(delivered_traces)}"
    )
    gens = support.spans_of_type(delivered_traces[0], "generation")
    assert len(gens) == 1, (
        f"expected exactly one generation span, got {len(gens)}"
    )
    return gens[0]


class TestCrossDbDiscrimination:
    def test_qoder_cn_db_wins_over_international_dbs(
        self, hook_env, delivered_traces, run_stop_hook
    ):
        """Both international DBs AND the qoder-cn DB exist: qoder-cn values used."""
        _create_intl_app_support_db(hook_env)
        _create_intl_dot_qoder_db(hook_env)
        _create_qoder_cn_db(hook_env)
        transcript = support.write_transcript(
            hook_env / ".qoder-cn" / "projects" / "proj-xdb" / f"{SESSION}.jsonl",
            _transcript(),
        )

        rc = run_stop_hook(support.stop_payload(SESSION, transcript))

        assert rc == 0, "hook must exit 0 with coexisting qoder-cn/international DBs"
        gen = _generation(delivered_traces)
        model = support.attr_str(gen, "langfuse.observation.model.name")
        assert model == QODER_CN_LOCAL_MODEL, (
            "token/model enrichment must come from the qoder-cn DB "
            f"({QODER_CN_LOCAL_MODEL}), not an international Qoder DB; "
            f"observed model: {model!r}"
        )
        usage = json.loads(
            support.attr_str(gen, "langfuse.observation.usage_details")
        )
        assert usage == {"input": 111, "output": 222}, (
            "token usage must come from the qoder-cn DB, not an international "
            f"Qoder DB; observed usage: {usage}"
        )

    @pytest.mark.skipif(sys.platform != "darwin", reason="macOS App Support candidate is darwin-only")
    def test_qoder_cn_app_support_candidate_found(
        self, hook_env, delivered_traces, run_stop_hook
    ):
        """macOS: qoder-cn App Support DB (com.qodercn.app.stable) is a candidate."""
        _create_intl_app_support_db(hook_env)
        _create_qoder_cn_app_support_db(hook_env)  # no ~/.qoder-cn DB at all
        transcript = support.write_transcript(
            hook_env / ".qoder-cn" / "projects" / "proj-xdb" / f"{SESSION}.jsonl",
            _transcript(),
        )

        rc = run_stop_hook(support.stop_payload(SESSION, transcript))

        assert rc == 0
        gen = _generation(delivered_traces)
        model = support.attr_str(gen, "langfuse.observation.model.name")
        assert model == QODER_CN_APP_SUPPORT_MODEL, (
            "the qoder-cn macOS App Support DB (com.qodercn.app.stable) must be "
            f"used when present; observed model: {model!r}"
        )

    def test_international_db_alone_is_never_read(
        self, hook_env, delivered_traces, run_stop_hook
    ):
        """Only international DBs exist: token/model must stay missing."""
        _create_intl_app_support_db(hook_env)
        _create_intl_dot_qoder_db(hook_env)
        transcript = support.write_transcript(
            hook_env / ".qoder-cn" / "projects" / "proj-xdb" / f"{SESSION}.jsonl",
            _transcript(),
        )

        rc = run_stop_hook(support.stop_payload(SESSION, transcript))

        assert rc == 0, "hook must exit 0 when no qoder-cn DB exists"
        gen = _generation(delivered_traces)
        model = support.attr_str(gen, "langfuse.observation.model.name")
        assert model == "unknown", (
            "no qoder-cn DB exists, so the model must fall back to 'unknown'; "
            f"observed model: {model!r} (an international Qoder DB was read)"
        )
        usage_attr = support.get_attr(gen, "langfuse.observation.usage_details")
        assert usage_attr is None, (
            "no qoder-cn DB exists, so usage_details must be absent; observed: "
            f"{usage_attr!r} (an international Qoder DB was read)"
        )
