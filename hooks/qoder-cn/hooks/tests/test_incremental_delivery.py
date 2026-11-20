"""R-1 observation 6: incremental delivery with per-session state.

Re-triggering Stop for the same session must deliver only the NEW transcript
content, with the incremental state file persisted under
``~/.qoder-cn/state/langfuse_state.json`` (sandbox home layout).

Distinguishes violating implementations: full redelivery on every trigger
(more payloads than new turns, or a later payload repeating earlier turn
content) and a missing/foreign state file location.
"""

import json

import support

SESSION = "sess-incremental"

TS1_USER = "2024-01-06T00:00:00Z"
TS1_ASSIST = "2024-01-06T00:00:01Z"
TS2_USER = "2024-01-06T00:01:00Z"
TS2_ASSIST = "2024-01-06T00:01:01Z"


def _turn_rows(prefix, user_ts, assist_ts):
    return [
        support.user_row(f"{prefix} question", user_ts),
        support.assistant_row(
            [support.text_block(f"{prefix} answer")], f"msg_{prefix}", assist_ts
        ),
    ]


def _state_file(hook_env):
    return hook_env / ".qoder-cn" / "state" / "langfuse_state.json"


def _read_state(hook_env):
    path = _state_file(hook_env)
    assert path.exists(), (
        f"incremental state file must live at ~/.qoder-cn/state/langfuse_state.json "
        f"(sandbox: {path})"
    )
    return json.loads(path.read_text(encoding="utf-8"))


class TestIncrementalDelivery:
    def test_retrigger_delivers_only_new_content(self, hook_env, delivered_traces, run_stop_hook):
        transcript = support.write_transcript(
            hook_env / ".qoder-cn" / "projects" / "proj-inc" / f"{SESSION}.jsonl",
            _turn_rows("turn-one", TS1_USER, TS1_ASSIST),
        )
        payload = support.stop_payload(SESSION, transcript)

        # --- trigger 1: the full first turn is delivered ---
        rc1 = run_stop_hook(payload)
        assert rc1 == 0, "first Stop trigger must exit 0"
        assert len(delivered_traces) == 1, (
            f"first trigger must deliver 1 trace, got {len(delivered_traces)}"
        )
        first_root = support.root_span(delivered_traces[0])
        first_input = json.loads(
            support.attr_str(first_root, "langfuse.observation.input")
        )
        assert first_input == {"role": "user", "content": "turn-one question"}

        state_after_1 = _read_state(hook_env)
        assert len(state_after_1) == 1, (
            "one session entry in the state file after the first trigger"
        )
        entry = next(iter(state_after_1.values()))
        assert entry["turn_count"] == 1, (
            f"state must track 1 delivered turn, got {entry['turn_count']}"
        )
        assert entry["offset"] == transcript.stat().st_size, (
            "state offset must cover everything read so far"
        )

        # --- trigger 2: only the appended second turn is delivered ---
        support.append_transcript_rows(
            transcript, _turn_rows("turn-two", TS2_USER, TS2_ASSIST)
        )
        rc2 = run_stop_hook(payload)
        assert rc2 == 0, "second Stop trigger must exit 0"
        assert len(delivered_traces) == 2, (
            "second trigger must deliver exactly one NEW trace (2 total), "
            f"got {len(delivered_traces)} — full redelivery would exceed 2"
        )
        second_root = support.root_span(delivered_traces[1])
        second_input = json.loads(
            support.attr_str(second_root, "langfuse.observation.input")
        )
        assert second_input == {"role": "user", "content": "turn-two question"}, (
            "the second delivery must carry the new turn's user message"
        )
        second_output = json.loads(
            support.attr_str(second_root, "langfuse.observation.output")
        )
        assert second_output == {"role": "assistant", "content": "turn-two answer"}, (
            "the second delivery must carry the new turn's assistant message"
        )
        # the second payload must not re-deliver the first turn
        second_dump = json.dumps(delivered_traces[1], ensure_ascii=False)
        assert "turn-one" not in second_dump, (
            "re-delivering earlier turn content would mean non-incremental delivery"
        )

        state_after_2 = _read_state(hook_env)
        entry = next(iter(state_after_2.values()))
        assert entry["turn_count"] == 2, (
            f"state must track 2 delivered turns, got {entry['turn_count']}"
        )
        assert entry["offset"] == transcript.stat().st_size, (
            "state offset must advance to the full transcript size"
        )

        # --- trigger 3: nothing new, nothing delivered ---
        rc3 = run_stop_hook(payload)
        assert rc3 == 0, "a no-op Stop trigger must exit 0"
        assert len(delivered_traces) == 2, (
            "a trigger with no new transcript content must not deliver anything"
        )

    def test_state_and_lock_live_in_qoder_cn_state_dir(self, hook_env, delivered_traces, run_stop_hook):
        """Hook state files (state json + lock) are created under
        ~/.qoder-cn/state/, never under ~/.qoder/state."""
        transcript = support.write_transcript(
            hook_env / ".qoder-cn" / "projects" / "proj-st" / f"{SESSION}.jsonl",
            _turn_rows("state-dir", TS1_USER, TS1_ASSIST),
        )

        rc = run_stop_hook(support.stop_payload(SESSION, transcript))

        assert rc == 0
        state_dir = hook_env / ".qoder-cn" / "state"
        assert _state_file(hook_env).exists(), "state json must be under ~/.qoder-cn/state/"
        assert (state_dir / "langfuse_state.lock").exists(), (
            "the lock file must be under ~/.qoder-cn/state/"
        )
        intl_state_dir = hook_env / ".qoder" / "state"
        assert not intl_state_dir.exists(), (
            "the hook must never write state into the international ~/.qoder/state"
        )
