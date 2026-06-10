#!/usr/bin/env python3
"""
Codex -> Langfuse hook

Parses Codex rollout JSONL and emits traces to Langfuse via three-tier delivery.
"""

import json
import logging
import os
import sys
import threading
import time
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# --- Delivery layer (langstash → direct push → failed log) ---
try:
    from langstash_deliver.deliver import deliver_trace
    from langstash_deliver.schema import build_trace_json, build_generation, build_span, Usage
    _HAS_DELIVER = True
except ImportError:
    _HAS_DELIVER = False

# --- Langfuse import (fail-open) ---
try:
    from langfuse import Langfuse, propagate_attributes
    from opentelemetry import trace as otel_trace_api
except Exception:
    sys.exit(0)

# --- Paths ---
STATE_DIR = Path.home() / ".codex" / "state"
LOG_FILE = STATE_DIR / "langfuse_hook.log"
STATE_FILE = STATE_DIR / "langfuse_state.json"
LOCK_FILE = STATE_DIR / "langfuse_state.lock"

DEBUG = (os.environ.get("LANGFUSE_DEBUG") or "true").lower() != "false"
try:
    MAX_CHARS = int(os.environ.get("LANGFUSE_MAX_CHARS") or "800000")
except ValueError:
    MAX_CHARS = 800000

# ----------------- Logging -----------------
_logger: Optional[logging.Logger] = None

def _get_logger() -> Optional[logging.Logger]:
    global _logger
    if _logger is not None:
        return _logger
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        lg = logging.getLogger("langfuse_hook")
        lg.setLevel(logging.DEBUG if DEBUG else logging.INFO)
        if not lg.handlers:
            from logging.handlers import RotatingFileHandler
            h = RotatingFileHandler(str(LOG_FILE), maxBytes=5_000_000, backupCount=3)
            h.setFormatter(logging.Formatter(
                "%(asctime)s [%(levelname)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            ))
            lg.addHandler(h)
        _logger = lg
        return _logger
    except Exception:
        return None

def debug(msg: str) -> None:
    if not DEBUG:
        return
    lg = _get_logger()
    if lg is not None:
        try:
            lg.debug(msg)
        except Exception:
            pass

def info(msg: str) -> None:
    lg = _get_logger()
    if lg is not None:
        try:
            lg.info(msg)
        except Exception:
            pass

# ----------------- State locking -----------------
class FileLock:
    def __init__(self, path: Path, timeout_s: float = 2.0):
        self.path = path
        self.timeout_s = timeout_s
        self._fh = None

    def __enter__(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+", encoding="utf-8")
        self.acquired = False
        try:
            import fcntl
        except ImportError:
            return self
        deadline = time.time() + self.timeout_s
        try:
            while True:
                try:
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.acquired = True
                    return self
                except BlockingIOError:
                    if time.time() > deadline:
                        raise TimeoutError(f"could not acquire {self.path} within {self.timeout_s}s")
                    time.sleep(0.05)
        except BaseException:
            try:
                self._fh.close()
            except Exception:
                pass
            raise

    def __exit__(self, exc_type, exc, tb):
        try:
            import fcntl
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            self._fh.close()
        except Exception:
            pass

def load_state() -> Dict[str, Any]:
    try:
        if not STATE_FILE.exists():
            return {}
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}

def save_state(state: Dict[str, Any]) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        debug(f"save_state failed: {e}")

def state_key(session_id: str, transcript_path: str) -> str:
    raw = f"{session_id}::{transcript_path}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

# ----------------- Hook payload -----------------
def read_hook_payload() -> Dict[str, Any]:
    try:
        data = sys.stdin.read()
        debug(f"stdin received {len(data)} chars")
        if not data.strip():
            return {}
        parsed = json.loads(data)
        if isinstance(parsed, dict):
            debug(f"payload top-level keys: {sorted(parsed.keys())}")
        return parsed
    except Exception as e:
        debug(f"read_hook_payload exception: {e!r}")
        return {}

def extract_session_and_transcript(payload: Dict[str, Any]) -> Tuple[Optional[str], Optional[Path]]:
    session_id = payload.get("session_id") or payload.get("sessionId")
    transcript = payload.get("transcript_path") or payload.get("transcriptPath")

    if transcript:
        try:
            transcript_path = Path(transcript).expanduser().resolve()
        except Exception:
            transcript_path = None
    else:
        transcript_path = None

    return session_id, transcript_path

# ----------------- Rollout data structures -----------------
@dataclass
class ToolCall:
    call_id: str
    name: str
    args: Any
    start_time: datetime
    end_time: Optional[datetime] = None
    output: Any = None
    error: Optional[str] = None

@dataclass
class ModelStep:
    start_time: datetime
    end_time: datetime
    reasoning: Optional[str] = None
    text: Optional[str] = None
    tool_calls: List[ToolCall] = field(default_factory=list)
    usage: Optional[Dict[str, int]] = None

@dataclass
class Turn:
    turn_id: Optional[str]
    start_time: datetime
    end_time: datetime
    model: Optional[str] = None
    user_input: Optional[str] = None
    final_output: Optional[str] = None
    steps: List[ModelStep] = field(default_factory=list)
    subagent_thread_ids: List[str] = field(default_factory=list)
    completed: bool = False
    aborted: bool = False
    total_usage: Optional[Dict[str, int]] = None

@dataclass
class SessionMeta:
    session_id: str = "unknown"
    cli_version: Optional[str] = None
    model_provider: Optional[str] = None
    base_instructions: Optional[str] = None

# ----------------- Rollout parser -----------------
def parse_timestamp(ts_str: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except Exception:
        return None

def parse_rollout(lines: List[Dict[str, Any]]) -> Tuple[SessionMeta, List[Turn]]:
    session_meta = SessionMeta()
    turns: List[Turn] = []

    current_turn: Optional[Turn] = None
    current_step: Optional[ModelStep] = None
    tool_calls_by_id: Dict[str, ToolCall] = {}
    last_timestamp = datetime.now(timezone.utc)

    def ensure_turn(ts: datetime) -> Turn:
        nonlocal current_turn
        if current_turn is None:
            current_turn = Turn(turn_id=None, start_time=ts, end_time=ts)
        return current_turn

    def ensure_step(ts: datetime) -> ModelStep:
        nonlocal current_step
        if current_step is None:
            current_step = ModelStep(start_time=ts, end_time=ts)
        return current_step

    def close_step(ts: datetime, usage: Optional[Dict[str, int]] = None):
        nonlocal current_step
        if current_step is None:
            return
        current_step.end_time = max(current_step.end_time, ts)
        if usage:
            current_step.usage = usage
        current_turn.steps.append(current_step)
        current_step = None

    def finish_turn(ts: datetime, completed: bool, aborted: bool):
        nonlocal current_turn, tool_calls_by_id
        if current_turn is None:
            return
        close_step(ts)
        current_turn.end_time = max(current_turn.end_time, ts)
        current_turn.completed = completed
        current_turn.aborted = aborted
        turns.append(current_turn)
        current_turn = None
        tool_calls_by_id = {}

    for line in lines:
        ts = parse_timestamp(line.get("timestamp", "")) or last_timestamp
        last_timestamp = ts
        line_type = line.get("type")
        payload = line.get("payload", {})

        if line_type == "session_meta":
            session_meta = SessionMeta(
                session_id=payload.get("id", session_meta.session_id),
                cli_version=payload.get("cli_version"),
                model_provider=payload.get("model_provider"),
                base_instructions=payload.get("base_instructions", {}).get("text") if isinstance(payload.get("base_instructions"), dict) else None,
            )
            continue

        if line_type == "turn_context":
            t = ensure_turn(ts)
            t.model = payload.get("model") or t.model
            continue

        if line_type == "response_item":
            ensure_turn(ts)
            p_type = payload.get("type")

            if p_type == "message":
                role = payload.get("role")
                content = payload.get("content", [])
                text = ""
                if isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and part.get("type") in ("input_text", "output_text", "text"):
                            text += part.get("text", "") + "\n"
                text = text.strip()

                if role == "assistant" and text:
                    step = ensure_step(ts)
                    step.text = (step.text + "\n" + text) if step.text else text
                elif role == "user" and text:
                    if not current_turn.user_input:
                        current_turn.user_input = text

            elif p_type in ("function_call", "custom_tool_call"):
                step = ensure_step(ts)
                call_id = payload.get("call_id")
                name = payload.get("name", "unknown")
                args_raw = payload.get("arguments") if p_type == "function_call" else payload.get("input")
                try:
                    args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                except Exception:
                    args = args_raw

                tc = ToolCall(call_id=call_id, name=name, args=args, start_time=ts)
                step.tool_calls.append(tc)
                tool_calls_by_id[call_id] = tc

            elif p_type in ("function_call_output", "custom_tool_call_output"):
                call_id = payload.get("call_id")
                tc = tool_calls_by_id.get(call_id)
                if tc:
                    if tc.output is None:
                        tc.output = payload.get("output")
                    tc.end_time = max(tc.end_time or ts, ts)

            elif p_type == "reasoning":
                content = payload.get("content")
                summary = payload.get("summary", [])
                reasoning_text = ""
                if isinstance(content, str):
                    reasoning_text = content
                elif isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and "text" in c:
                            reasoning_text += str(c["text"]) + "\n"
                elif isinstance(summary, list):
                    for s in summary:
                        reasoning_text += str(s) + "\n"

                if reasoning_text.strip():
                    step = ensure_step(ts)
                    step.reasoning = (step.reasoning + "\n" + reasoning_text) if step.reasoning else reasoning_text
            continue

        if line_type == "event_msg":
            et = payload.get("type")

            if et == "task_started":
                if current_turn:
                    finish_turn(ts, completed=False, aborted=False)
                current_turn = Turn(turn_id=payload.get("turn_id"), start_time=ts, end_time=ts)
                continue

            ensure_turn(ts)

            if et == "user_message":
                msg = payload.get("message")
                if isinstance(msg, str) and not current_turn.user_input:
                    current_turn.user_input = msg

            elif et == "agent_message":
                msg = payload.get("message")
                if isinstance(msg, str):
                    current_turn.final_output = msg

            elif et == "token_count":
                info_data = payload.get("info", {})
                if info_data:
                    total_usage = info_data.get("total_token_usage")
                    if total_usage:
                        current_turn.total_usage = total_usage
                    last_usage = info_data.get("last_token_usage")
                    close_step(ts, last_usage)

            elif et == "task_complete":
                finish_turn(ts, completed=True, aborted=False)

            elif et == "turn_aborted":
                finish_turn(ts, completed=True, aborted=True)

            else:
                if et == "collab_agent_spawn_end":
                    new_thread_id = payload.get("new_thread_id")
                    if isinstance(new_thread_id, str):
                        current_turn.subagent_thread_ids.append(new_thread_id)

                call_id = payload.get("call_id")
                if isinstance(call_id, str) and et.endswith("_end"):
                    tc = tool_calls_by_id.get(call_id)
                    if tc:
                        tc.end_time = max(tc.end_time or ts, ts)
                        status = payload.get("status")
                        if status in ("failed", "declined"):
                            error_msg = payload.get("error") or payload.get("stderr") or payload.get("stdout")
                            tc.error = str(error_msg) if error_msg else f"Status: {status}"
                        if tc.output is None:
                            tc.output = payload.get("aggregated_output") or payload.get("stdout")
            continue

    if current_turn:
        finish_turn(last_timestamp, completed=False, aborted=False)

    return session_meta, turns

# ----------------- Incremental reader -----------------
@dataclass
class SessionState:
    offset: int = 0
    buffer: str = ""
    turn_count: int = 0

def load_session_state(global_state: Dict[str, Any], key: str) -> SessionState:
    s = global_state.get(key, {})
    return SessionState(
        offset=int(s.get("offset", 0)),
        buffer=str(s.get("buffer", "")),
        turn_count=int(s.get("turn_count", 0)),
    )

def write_session_state(global_state: Dict[str, Any], key: str, ss: SessionState) -> None:
    global_state[key] = {
        "offset": ss.offset,
        "buffer": ss.buffer,
        "turn_count": ss.turn_count,
        "updated": datetime.now(timezone.utc).isoformat(),
    }

def read_new_jsonl(transcript_path: Path, ss: SessionState) -> Tuple[List[Dict[str, Any]], SessionState]:
    if not transcript_path.exists():
        return [], ss

    try:
        file_size = transcript_path.stat().st_size
        if file_size < ss.offset:
            debug(f"transcript shrank ({file_size} < {ss.offset}); restarting")
            ss.offset = 0
            ss.buffer = ""
        with open(transcript_path, "rb") as f:
            f.seek(ss.offset)
            chunk = f.read()
            new_offset = f.tell()
    except Exception as e:
        debug(f"read_new_jsonl failed: {e}")
        return [], ss

    if not chunk:
        return [], ss

    try:
        text = chunk.decode("utf-8", errors="replace")
    except Exception:
        text = chunk.decode(errors="replace")

    combined = ss.buffer + text
    lines = combined.split("\n")
    ss.buffer = lines[-1]
    ss.offset = new_offset

    msgs: List[Dict[str, Any]] = []
    for line in lines[:-1]:
        line = line.strip()
        if not line:
            continue
        try:
            msgs.append(json.loads(line))
        except Exception:
            continue

    return msgs, ss

# ----------------- Sidecar dedup -----------------
def load_sidecar(rollout_path: Path) -> set:
    sidecar_path = rollout_path.with_suffix(rollout_path.suffix + ".langfuse")
    if not sidecar_path.exists():
        return set()
    try:
        return set(sidecar_path.read_text(encoding="utf-8").splitlines())
    except Exception:
        return set()

def mark_uploaded(rollout_path: Path, turn_id: str) -> None:
    sidecar_path = rollout_path.with_suffix(rollout_path.suffix + ".langfuse")
    try:
        with open(sidecar_path, "a", encoding="utf-8") as f:
            f.write(turn_id + "\n")
    except Exception as e:
        debug(f"mark_uploaded failed: {e}")

# ----------------- Trace Schema v2 builder -----------------
def build_trace_v2(
    session_meta: SessionMeta,
    turn_num: int,
    turn: Turn,
    transcript_path: Path,
    user_id: Optional[str],
    tags: List[str],
) -> Dict[str, Any]:
    trace_label = f"Codex - Turn {turn_num}"

    generations: List[Dict[str, Any]] = []
    spans: List[Dict[str, Any]] = []

    for idx, step in enumerate(turn.steps):
        if idx == 0:
            gen_input = {"role": "user", "content": turn.user_input} if turn.user_input else None
        else:
            prev_step = turn.steps[idx - 1]
            if prev_step.tool_calls:
                tool_results = [
                    {"name": tc.name, "output": str(tc.output) if tc.output else None}
                    for tc in prev_step.tool_calls
                ]
                gen_input = {"role": "tool", "tool_results": tool_results}
            else:
                gen_input = None

        gen_output: Dict[str, Any] = {"role": "assistant"}
        if step.text:
            gen_output["content"] = step.text
        if step.reasoning:
            gen_output["reasoning"] = step.reasoning
        if step.tool_calls:
            gen_output["tool_calls"] = [
                {"id": tc.call_id, "name": tc.name, "arguments": tc.args}
                for tc in step.tool_calls
            ]

        usage: Optional[Usage] = None
        if step.usage:
            usage = {}
            if "input_tokens" in step.usage:
                usage["input"] = step.usage["input_tokens"]
            if "output_tokens" in step.usage:
                usage["output"] = step.usage["output_tokens"]
            if "cached_input_tokens" in step.usage:
                usage["cache_read_input_tokens"] = step.usage["cached_input_tokens"]

        gen = build_generation(
            name=f"Codex Generation {idx + 1}",
            model=turn.model or "unknown",
            start_time=step.start_time,
            end_time=step.end_time,
            gen_input=gen_input,
            gen_output=gen_output,
            usage=usage,
            metadata={"step_index": idx},
        )
        generations.append(gen)

        for tc in step.tool_calls:
            tc_input = tc.args
            if isinstance(tc_input, str):
                tc_input = tc_input[:MAX_CHARS] if len(tc_input) > MAX_CHARS else tc_input

            tc_output = str(tc.output) if tc.output else None
            if tc_output and len(tc_output) > MAX_CHARS:
                tc_output = tc_output[:MAX_CHARS]

            span = build_span(
                name=f"Tool: {tc.name}",
                generation_index=idx,
                start_time=tc.start_time,
                end_time=tc.end_time or step.end_time,
                span_input=tc_input,
                span_output=tc_output,
                metadata={
                    "call_id": tc.call_id,
                    **({"error": tc.error} if tc.error else {}),
                },
            )
            spans.append(span)

    return build_trace_json(
        source="codex",
        session_id=session_meta.session_id,
        user_id=user_id,
        tags=tags,
        trace_name=trace_label,
        start_time=turn.start_time,
        end_time=turn.end_time,
        user_input={"role": "user", "content": turn.user_input} if turn.user_input else None,
        assistant_output={"role": "assistant", "content": turn.final_output} if turn.final_output else None,
        metadata={
            "source": "codex",
            "turn_number": turn_num,
            "turn_id": turn.turn_id,
            "model": turn.model,
            "model_provider": session_meta.model_provider,
            "cli_version": session_meta.cli_version,
            "aborted": turn.aborted,
            "tool_call_count": sum(len(s.tool_calls) for s in turn.steps),
            "subagent_thread_ids": turn.subagent_thread_ids,
            "transcript_path": str(transcript_path),
        },
        generations=generations,
        spans=spans,
    )

# ----------------- Direct Langfuse emit (fallback) -----------------
def _to_ns(ts: Optional[datetime]) -> Optional[int]:
    if ts is None:
        return None
    return int(ts.timestamp() * 1_000_000_000)

def _start_backdated(
    langfuse: Langfuse,
    *,
    name: str,
    as_type: str,
    start_time: Optional[datetime],
    parent_otel_span: Any = None,
    **obs_kwargs: Any,
) -> Any:
    if not hasattr(langfuse, "_otel_tracer") or not hasattr(langfuse, "_create_observation_from_otel_span"):
        raise RuntimeError("Langfuse SDK missing required internals")
    start_ns = _to_ns(start_time)
    if parent_otel_span is not None:
        with otel_trace_api.use_span(parent_otel_span, end_on_exit=False):
            otel_span = langfuse._otel_tracer.start_span(name=name, start_time=start_ns)
    else:
        otel_span = langfuse._otel_tracer.start_span(name=name, start_time=start_ns)
    return langfuse._create_observation_from_otel_span(
        otel_span=otel_span,
        as_type=as_type,
        **obs_kwargs,
    )

def emit_turn_direct(
    langfuse: Langfuse,
    session_meta: SessionMeta,
    turn_num: int,
    turn: Turn,
    transcript_path: Path,
    user_id: Optional[str] = None,
    tags: Optional[List[str]] = None,
) -> None:
    trace_label = f"Codex - Turn {turn_num}"

    pa_kwargs: Dict[str, Any] = {
        "session_id": session_meta.session_id,
        "trace_name": trace_label,
        "tags": tags or ["codex"],
    }
    if user_id:
        pa_kwargs["user_id"] = user_id

    with propagate_attributes(**pa_kwargs):
        trace_span = _start_backdated(
            langfuse,
            name=trace_label,
            as_type="span",
            start_time=turn.start_time,
            input={"role": "user", "content": turn.user_input} if turn.user_input else None,
            metadata={
                "source": "codex",
                "turn_id": turn.turn_id,
                "model": turn.model,
            },
        )
        parent_otel_span = trace_span._otel_span

        for idx, step in enumerate(turn.steps):
            if idx == 0:
                gen_input = {"role": "user", "content": turn.user_input} if turn.user_input else None
            else:
                gen_input = None

            gen_output: Dict[str, Any] = {"role": "assistant"}
            if step.text:
                gen_output["content"] = step.text
            if step.tool_calls:
                gen_output["tool_calls"] = [
                    {"id": tc.call_id, "name": tc.name, "arguments": tc.args}
                    for tc in step.tool_calls
                ]

            gen_span = _start_backdated(
                langfuse,
                name=f"Codex Generation {idx + 1}",
                as_type="generation",
                start_time=step.start_time,
                parent_otel_span=parent_otel_span,
                model=turn.model,
                input=gen_input,
                output=gen_output,
                metadata={"step_index": idx},
            )

            for tc in step.tool_calls:
                tool_span = _start_backdated(
                    langfuse,
                    name=f"Tool: {tc.name}",
                    as_type="tool",
                    start_time=tc.start_time,
                    parent_otel_span=gen_span._otel_span,
                    input=tc.args,
                    metadata={"call_id": tc.call_id},
                )
                tool_span.update(output=str(tc.output) if tc.output else None)
                tool_span.end(end_time=_to_ns(tc.end_time or step.end_time))

            gen_span.end(end_time=_to_ns(step.end_time))

        trace_span.update(output={"role": "assistant", "content": turn.final_output} if turn.final_output else None)
        trace_span.end(end_time=_to_ns(turn.end_time))

# ----------------- Main -----------------
def resolve_user_id() -> Optional[str]:
    uid = os.environ.get("LANGFUSE_USER_ID")
    if uid:
        return uid
    for k in ("USER", "LOGNAME", "USERNAME"):
        v = os.environ.get(k)
        if v:
            return v
    try:
        import getpass
        return getpass.getuser()
    except Exception:
        return None

def main() -> int:
    start = time.time()
    debug("Hook started")

    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY", "")
    host = os.environ.get("LANGFUSE_BASE_URL") or "https://us.cloud.langfuse.com"

    if not public_key or not secret_key:
        return 0

    payload = read_hook_payload()
    session_id, transcript_path = extract_session_and_transcript(payload)

    if not session_id or not transcript_path:
        debug("Missing session_id or transcript_path from hook payload; exiting.")
        return 0

    if not transcript_path.exists():
        debug(f"Transcript path does not exist: {transcript_path}")
        return 0

    user_id = resolve_user_id()
    tags_raw = os.environ.get("LANGFUSE_TAGS") or "codex"
    tags = [t.strip() for t in tags_raw.split(",") if t.strip()]

    langfuse = None
    try:
        langfuse = Langfuse(public_key=public_key, secret_key=secret_key, host=host)
    except Exception:
        return 0

    try:
        with FileLock(LOCK_FILE):
            state = load_state()
            key = state_key(session_id, str(transcript_path))
            ss = load_session_state(state, key)

            msgs, ss = read_new_jsonl(transcript_path, ss)
            if not msgs:
                write_session_state(state, key, ss)
                save_state(state)
                return 0

            session_meta, turns = parse_rollout(msgs)
            if not turns:
                write_session_state(state, key, ss)
                save_state(state)
                return 0

            sidecar = load_sidecar(transcript_path)

            emitted = 0
            for t in turns:
                if t.completed and t.turn_id and t.turn_id in sidecar:
                    continue

                emitted += 1
                turn_num = ss.turn_count + emitted

                if _HAS_DELIVER:
                    try:
                        trace_json = build_trace_v2(
                            session_meta, turn_num, t, transcript_path,
                            user_id=user_id, tags=tags,
                        )
                    except Exception as e:
                        debug(f"build_trace_v2 failed: {e}")
                        trace_json = None

                    def _direct_push(_tj, _t=t, _turn_num=turn_num):
                        emit_turn_direct(
                            langfuse, session_meta, _turn_num, _t, transcript_path,
                            user_id=user_id, tags=tags,
                        )
                        return True

                    if trace_json:
                        deliver_trace(trace_json, direct_push_fn=_direct_push)
                    else:
                        _direct_push(None)
                else:
                    try:
                        emit_turn_direct(
                            langfuse, session_meta, turn_num, t, transcript_path,
                            user_id=user_id, tags=tags,
                        )
                    except Exception as e:
                        info(f"emit_turn_direct failed: {type(e).__name__}: {e}")

                if t.completed and t.turn_id:
                    mark_uploaded(transcript_path, t.turn_id)

            ss.turn_count += emitted
            write_session_state(state, key, ss)
            save_state(state)

        dur = time.time() - start
        info(f"Processed {emitted} turns in {dur:.2f}s (session={session_id})")
        return 0

    except TimeoutError as e:
        debug(f"lock timeout, skipping: {e}")
        return 0

    except Exception as e:
        debug(f"Unexpected failure: {e}")
        return 0

    finally:
        if langfuse is not None:
            try:
                def _flush_and_shutdown():
                    try:
                        langfuse.flush()
                    except Exception:
                        pass
                    langfuse.shutdown()
                t = threading.Thread(target=_flush_and_shutdown, daemon=True)
                t.start()
                t.join(5.0)
            except Exception:
                pass

if __name__ == "__main__":
    sys.exit(main())
