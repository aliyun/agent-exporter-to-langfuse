#!/usr/bin/env python3
"""
QoderWork -> Langfuse hook

"""

import json
import logging
import os
import sys
import time
import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, TypedDict

# --- Delivery layer (langstash → Langfuse OTel → failed log) ---
try:
    from langstash_deliver.deliver import deliver_trace
    _HAS_DELIVER = True
except ImportError:
    _HAS_DELIVER = False

# --- OTel SDK for building OTLP JSON ---
try:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry import trace as otel_trace_api, context as otel_context_api
except Exception:
    sys.exit(0)


# NOTE: QoderWork does not currently provide token usage in transcript (no message.usage)
# and does not write to a local SQLite DB. Token/usage data will be available once the
# QoderWork team adds message.usage support. The get_usage() function below is kept so
# that when message.usage becomes available, tokens will be captured automatically.

# --- Paths ---
STATE_DIR = Path.home() / ".qoderwork" / "state"
LOG_FILE = STATE_DIR / "langfuse_hook.log"
STATE_FILE = STATE_DIR / "langfuse_state.json"
LOCK_FILE = STATE_DIR / "langfuse_state.lock"

def _opt(name: str) -> str:
    """Read a configuration value from environment variables."""
    return os.environ.get(name) or ""

DEBUG = _opt("LANGFUSE_DEBUG").lower() != "false"
try:
    MAX_CHARS = int(_opt("LANGFUSE_MAX_CHARS") or "800000")
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
        lg = logging.getLogger("qoderwork_langfuse_hook")
        lg.setLevel(logging.DEBUG if DEBUG else logging.INFO)
        if not lg.handlers:
            h = RotatingFileHandler(str(LOG_FILE), maxBytes=200_000_000, backupCount=3)
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

def warn(msg: str) -> None:
    lg = _get_logger()
    if lg is not None:
        try:
            lg.warning(msg)
        except Exception:
            pass

# ----------------- State locking (best-effort) -----------------
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
            import fcntl  # Unix only
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
                        raise TimeoutError(
                            f"could not acquire {self.path} within {self.timeout_s}s"
                        )
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
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        for k in list(state.keys()):
            entry = state.get(k)
            if not isinstance(entry, dict):
                continue
            updated = entry.get("updated")
            if not isinstance(updated, str):
                continue
            try:
                ts = datetime.fromisoformat(updated.replace("Z", "+00:00"))
            except Exception:
                continue
            if ts < cutoff:
                del state[k]
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
    """
    QoderWork hooks pass a JSON payload on stdin.
    This script tolerates missing/empty stdin by returning {}.
    """
    try:
        data = sys.stdin.read()
        debug(f"stdin received {len(data)} chars")
        if not data.strip():
            return {}
        parsed = json.loads(data)
        if isinstance(parsed, dict):
            debug(f"payload top-level keys: {sorted(parsed.keys())}")
            debug(f"PAYLOAD_DUMP: {json.dumps(parsed, ensure_ascii=False, default=str)}")
        return parsed
    except Exception as e:
        debug(f"read_hook_payload exception: {e!r}")
        return {}

@dataclass
class HookContext:
    session_id: Optional[str] = None
    transcript_path: Optional[Path] = None
    hook_event: Optional[str] = None
    cwd: Optional[str] = None
    # SubagentStop context
    agent_id: Optional[str] = None
    agent_type: Optional[str] = None
    # extra context (Desktop payload)
    branch: Optional[str] = None
    repo: Optional[str] = None
    email: Optional[str] = None
    user_name: Optional[str] = None
    user_uid: Optional[str] = None
    org_name: Optional[str] = None

def extract_hook_context(payload: Dict[str, Any]) -> HookContext:
    ctx = HookContext()
    ctx.session_id = payload.get("session_id")
    ctx.hook_event = payload.get("hook_event_name")
    ctx.cwd = payload.get("cwd")

    if not ctx.session_id:
        warn("payload missing session_id")
    if ctx.hook_event and ctx.hook_event not in ("Stop", "SubagentStop"):
        warn(f"unexpected hook_event_name: {ctx.hook_event}")

    if ctx.hook_event == "SubagentStop":
        transcript = payload.get("agent_transcript_path")
        if not transcript:
            debug("SubagentStop has no agent_transcript_path (Desktop), skipping")
            return ctx
    else:
        transcript = payload.get("transcript_path")
        if not transcript:
            warn("payload missing transcript_path")

    if transcript:
        try:
            ctx.transcript_path = Path(transcript).expanduser().resolve()
        except Exception:
            warn(f"failed to resolve transcript_path: {transcript}")

    if ctx.hook_event == "SubagentStop":
        ctx.agent_id = payload.get("agent_id") or None
        ctx.agent_type = payload.get("agent_type") or None

    extra = payload.get("extra")
    if isinstance(extra, dict):
        ctx.branch = extra.get("branch") or None
        ctx.repo = extra.get("repo") or None
        ctx.email = extra.get("email") or None
        user = extra.get("user")
        if isinstance(user, dict):
            ctx.user_name = user.get("name") or None
            ctx.user_uid = user.get("uid") or None
            ctx.org_name = user.get("org_name") or None
            if not ctx.email:
                ctx.email = user.get("email") or None

    return ctx

# ----------------- Transcript parsing helpers -----------------
def get_content(msg: Dict[str, Any]) -> Any:
    if not isinstance(msg, dict):
        return None
    if "message" in msg and isinstance(msg.get("message"), dict):
        return msg["message"].get("content")
    return msg.get("content")

def get_role(msg: Dict[str, Any]) -> Optional[str]:
    t = msg.get("type")
    if t in ("user", "assistant"):
        return t
    m = msg.get("message")
    if isinstance(m, dict):
        r = m.get("role")
        if r in ("user", "assistant"):
            return r
    return None

def is_tool_result(msg: Dict[str, Any]) -> bool:
    role = get_role(msg)
    if role != "user":
        return False
    content = get_content(msg)
    if isinstance(content, list):
        return any(isinstance(x, dict) and x.get("type") == "tool_result" for x in content)
    return False

def iter_tool_results(content: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if isinstance(content, list):
        for x in content:
            if isinstance(x, dict) and x.get("type") == "tool_result":
                out.append(x)
    return out

def iter_tool_uses(content: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if isinstance(content, list):
        for x in content:
            if isinstance(x, dict) and x.get("type") == "tool_use":
                out.append(x)
    return out

def extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for x in content:
            if isinstance(x, dict) and x.get("type") == "text":
                parts.append(x.get("text", ""))
            elif isinstance(x, str):
                parts.append(x)
        return "\n".join([p for p in parts if p])
    return ""

def truncate_text(s: str, max_chars: int = MAX_CHARS) -> Tuple[str, Dict[str, Any]]:
    if s is None:
        return "", {"truncated": False, "orig_len": 0}
    orig_len = len(s)
    if orig_len <= max_chars:
        return s, {"truncated": False, "orig_len": orig_len}
    head = s[:max_chars]
    return head, {"truncated": True, "orig_len": orig_len, "kept_len": len(head), "sha256": hashlib.sha256(s.encode("utf-8")).hexdigest()}

def get_model(msg: Dict[str, Any]) -> str:
    m = msg.get("message")
    if isinstance(m, dict):
        v = m.get("model")
        if v and isinstance(v, str):
            return v
    return ""

class Usage(TypedDict, total=False):
    input: int
    output: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int

def get_usage(msg: Dict[str, Any]) -> Optional["Usage"]:
    m = msg.get("message")
    if not isinstance(m, dict):
        return None
    u = m.get("usage")
    if not isinstance(u, dict):
        return None
    details: Usage = {}
    for src, dst in (
        ("input_tokens", "input"),
        ("output_tokens", "output"),
        ("cache_read_input_tokens", "cache_read_input_tokens"),
        ("cache_creation_input_tokens", "cache_creation_input_tokens"),
    ):
        v = u.get(src)
        if isinstance(v, int) and v > 0:
            details[dst] = v
    return details or None

def get_message_id(msg: Dict[str, Any]) -> Optional[str]:
    m = msg.get("message")
    if isinstance(m, dict):
        mid = m.get("id")
        if isinstance(mid, str) and mid:
            return mid
    return None

def parse_ts(value: Any) -> Optional[datetime]:
    if isinstance(value, dict):
        value = value.get("timestamp")
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None

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

# ----------------- Turn assembly -----------------
@dataclass
class Turn:
    user_msg: Dict[str, Any]
    assistant_msgs: List[Dict[str, Any]]
    tool_results_by_id: Dict[str, Any]

def _merge_assistant_content(existing: Dict[str, Any], new_row: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(existing)
    old_content = get_content(merged)
    new_content = get_content(new_row)

    if not isinstance(old_content, list):
        old_content = [old_content] if old_content else []
    if not isinstance(new_content, list):
        new_content = [new_content] if new_content else []

    combined = list(old_content) + list(new_content)
    if "message" in merged and isinstance(merged["message"], dict):
        merged["message"] = dict(merged["message"])
        merged["message"]["content"] = combined
        new_msg = new_row.get("message")
        if isinstance(new_msg, dict):
            for key in ("usage", "model", "stop_reason"):
                val = new_msg.get(key)
                if val is not None:
                    merged["message"][key] = val
    if "timestamp" in new_row:
        merged["end_timestamp"] = new_row["timestamp"]
    return merged

def build_turns(messages: List[Dict[str, Any]]) -> List[Turn]:
    turns: List[Turn] = []
    current_user: Optional[Dict[str, Any]] = None

    assistant_order: List[str] = []
    assistant_latest: Dict[str, Dict[str, Any]] = {}
    noid_group: int = 0
    last_was_noid_assistant: bool = False

    tool_results_by_id: Dict[str, Any] = {}

    def flush_turn():
        nonlocal current_user, assistant_order, assistant_latest, tool_results_by_id, turns
        if current_user is None:
            return
        if not assistant_latest:
            warn("turn has user message but no assistant messages, skipping")
            return
        assistants = [assistant_latest[mid] for mid in assistant_order if mid in assistant_latest]
        for am in assistants:
            for tu in iter_tool_uses(get_content(am)):
                tid = tu.get("id")
                if tid and str(tid) not in tool_results_by_id:
                    warn(f"tool_use {tu.get('name')}({tid}) has no matching tool_result")
        turns.append(Turn(user_msg=current_user, assistant_msgs=assistants, tool_results_by_id=dict(tool_results_by_id)))

    for msg in messages:
        role = get_role(msg)

        if is_tool_result(msg):
            last_was_noid_assistant = False
            row_ts = msg.get("timestamp")
            for tr in iter_tool_results(get_content(msg)):
                tid = tr.get("tool_use_id")
                if tid:
                    tool_results_by_id[str(tid)] = {"content": tr.get("content"), "timestamp": row_ts}
            continue

        if role == "user":
            last_was_noid_assistant = False
            flush_turn()

            current_user = msg
            assistant_order = []
            assistant_latest = {}
            noid_group = 0
            tool_results_by_id = {}
            continue

        if role == "assistant":
            if current_user is None:
                continue

            mid = get_message_id(msg)
            if mid is None:
                if not last_was_noid_assistant:
                    noid_group += 1
                mid = f"noid:{noid_group}"
                last_was_noid_assistant = True
            else:
                last_was_noid_assistant = False

            if mid not in assistant_latest:
                assistant_order.append(mid)
                assistant_latest[mid] = msg
            else:
                assistant_latest[mid] = _merge_assistant_content(assistant_latest[mid], msg)
            continue

    flush_turn()
    return turns

# ----------------- OTLP JSON builder -----------------

SCOPE_NAME = "agent-exporter-to-langfuse"


def _to_ns(ts: Optional[datetime]) -> Optional[int]:
    if ts is None:
        return None
    return int(ts.timestamp() * 1_000_000_000)


def _make_attr(key: str, value: Any) -> Dict[str, Any]:
    if isinstance(value, str):
        return {"key": key, "value": {"stringValue": value}}
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    return {"key": key, "value": {"stringValue": json.dumps(value, ensure_ascii=False)}}


def _spans_to_otlp_json(exporter: InMemorySpanExporter) -> Dict[str, Any]:
    spans_data = []
    for span in exporter.get_finished_spans():
        ctx = span.get_span_context()
        trace_id = format(ctx.trace_id, "032x")
        span_id = format(ctx.span_id, "016x")
        parent_id = ""
        if span.parent is not None:
            parent_id = format(span.parent.span_id, "016x")

        attributes = []
        if span.attributes:
            for k, v in span.attributes.items():
                attributes.append(_make_attr(k, v))

        span_dict: Dict[str, Any] = {
            "traceId": trace_id,
            "spanId": span_id,
            "name": span.name,
            "startTimeUnixNano": str(span.start_time),
            "endTimeUnixNano": str(span.end_time or span.start_time),
        }
        if parent_id:
            span_dict["parentSpanId"] = parent_id
        if attributes:
            span_dict["attributes"] = attributes

        spans_data.append(span_dict)

    return {
        "resourceSpans": [{
            "scopeSpans": [{
                "scope": {"name": SCOPE_NAME},
                "spans": spans_data,
            }],
        }],
    }


def build_otlp_json(ctx: HookContext, turn_num: int, turn: Turn,
                     user_id: Optional[str], tags: List[str]) -> Dict[str, Any]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({}))
    provider.add_span_processor(
        __import__("opentelemetry.sdk.trace.export", fromlist=["SimpleSpanProcessor"]).SimpleSpanProcessor(exporter)
    )
    tracer = provider.get_tracer(SCOPE_NAME)

    user_text_raw = extract_text(get_content(turn.user_msg))
    user_text, _ = truncate_text(user_text_raw)
    last_assistant = turn.assistant_msgs[-1]
    final_text, _ = truncate_text(extract_text(get_content(last_assistant)))

    user_ts = parse_ts(turn.user_msg)
    last_ts = parse_ts(last_assistant)
    candidate_end = [t for t in [last_ts] if t is not None]
    for tr in turn.tool_results_by_id.values():
        t = parse_ts(tr)
        if t is not None:
            candidate_end.append(t)
    turn_end = max(candidate_end) if candidate_end else None

    is_subagent = ctx.hook_event == "SubagentStop"
    trace_label = f"QoderWork - Subagent Turn {turn_num}" if is_subagent else f"QoderWork - Turn {turn_num}"

    root_start_ns = _to_ns(user_ts) or _to_ns(datetime.now(timezone.utc))
    root_end_ns = _to_ns(turn_end or last_ts or user_ts) or root_start_ns

    root_attrs = {
        "langfuse.trace.name": trace_label,
        "session.id": ctx.session_id,
        "langfuse.observation.input": json.dumps({"role": "user", "content": user_text}, ensure_ascii=False),
        "langfuse.observation.output": json.dumps({"role": "assistant", "content": final_text}, ensure_ascii=False),
    }
    if user_id:
        root_attrs["user.id"] = user_id
    if tags:
        root_attrs["langfuse.trace.tags"] = json.dumps(tags)

    root_span = tracer.start_span(name=trace_label, start_time=root_start_ns, attributes=root_attrs)
    root_ctx = otel_trace_api.set_span_in_context(root_span)

    prev_ts = user_ts
    for idx, am in enumerate(turn.assistant_msgs):
        am_ts = parse_ts(am)
        am_text, _ = truncate_text(extract_text(get_content(am)))
        model = get_model(am)
        tool_uses = iter_tool_uses(get_content(am))

        if idx == 0:
            gen_input: Any = {"role": "user", "content": user_text}
        else:
            gen_input = None

        gen_tool_calls = []
        for tu in tool_uses:
            tu_inp = tu.get("input")
            if isinstance(tu_inp, str):
                tu_inp, _ = truncate_text(tu_inp)
            gen_tool_calls.append({"id": tu.get("id"), "name": tu.get("name"), "input": tu_inp})

        gen_output: Dict[str, Any] = {"role": "assistant"}
        if am_text:
            gen_output["content"] = am_text
        if gen_tool_calls:
            gen_output["tool_calls"] = gen_tool_calls

        usage = get_usage(am)

        gen_attrs: Dict[str, Any] = {
            "langfuse.observation.type": "generation",
            "langfuse.observation.model.name": model,
            "langfuse.observation.input": json.dumps(gen_input, ensure_ascii=False) if gen_input else "",
            "langfuse.observation.output": json.dumps(gen_output, ensure_ascii=False),
        }
        if usage:
            gen_attrs["langfuse.observation.usage_details"] = json.dumps(usage, ensure_ascii=False)

        gen_start_ns = _to_ns(prev_ts or am_ts) or root_start_ns
        gen_span = tracer.start_span(
            name=f"QoderWork Generation {idx + 1}", start_time=gen_start_ns,
            attributes=gen_attrs, context=root_ctx,
        )
        gen_ctx = otel_trace_api.set_span_in_context(gen_span)

        batch_end: List[datetime] = []
        for tu in tool_uses:
            tid = str(tu.get("id") or "")
            tname = tu.get("name") or "unknown"
            tinput_raw = tu.get("input") if isinstance(tu.get("input"), (dict, list, str, int, float, bool)) else {}
            if isinstance(tinput_raw, str):
                tinput, _ = truncate_text(tinput_raw)
            else:
                tinput = tinput_raw
            tr_entry = turn.tool_results_by_id.get(tid)
            out_trunc, tr_ts = None, None
            if tr_entry:
                out_raw = tr_entry.get("content")
                out_str = out_raw if isinstance(out_raw, str) else json.dumps(out_raw, ensure_ascii=False)
                out_trunc, _ = truncate_text(out_str)
                tr_ts = parse_ts(tr_entry.get("timestamp"))
            if tr_ts:
                batch_end.append(tr_ts)

            tool_attrs: Dict[str, Any] = {
                "langfuse.observation.type": "tool",
                "langfuse.observation.input": json.dumps(tinput, ensure_ascii=False) if tinput else "",
                "langfuse.observation.metadata.tool_name": tname,
                "langfuse.observation.metadata.tool_id": tid,
            }
            if out_trunc:
                tool_attrs["langfuse.observation.output"] = out_trunc

            tool_start_ns = _to_ns(am_ts) or gen_start_ns
            tool_end_ns = _to_ns(tr_ts or am_ts) or tool_start_ns
            tool_span = tracer.start_span(
                name=f"Tool: {tname}", start_time=tool_start_ns,
                attributes=tool_attrs, context=gen_ctx,
            )
            tool_span.end(end_time=tool_end_ns)

        gen_end_ts = max(batch_end) if batch_end else am_ts
        gen_span.end(end_time=_to_ns(gen_end_ts or am_ts or prev_ts) or gen_start_ns)

        if batch_end:
            prev_ts = max(batch_end)
        elif am_ts:
            prev_ts = am_ts

    root_span.end(end_time=root_end_ns)
    provider.shutdown()

    return _spans_to_otlp_json(exporter)

def resolve_user_id(ctx: HookContext) -> Optional[str]:
    uid = _opt("LANGFUSE_USER_ID")
    if uid:
        return uid
    if ctx.user_name:
        return ctx.user_name
    for k in ("USER", "LOGNAME", "USERNAME"):
        v = os.environ.get(k)
        if v:
            return v
    try:
        import getpass
        return getpass.getuser()
    except Exception:
        return None

# ----------------- Main -----------------
def _log_network_info() -> None:
    """Log network info to help diagnose langstash connectivity from inside a VM."""
    import platform
    import subprocess
    uname = platform.system()
    debug(f"uname={uname}")
    if uname == "Linux":
        try:
            gw = subprocess.check_output(
                ["ip", "route"], timeout=2, stderr=subprocess.DEVNULL
            ).decode().strip()
            for line in gw.splitlines():
                if line.startswith("default"):
                    debug(f"default gateway: {line}")
                    break
        except Exception:
            pass
    langstash_url = os.environ.get("LANGSTASH_URL", "")
    langstash_enabled = os.environ.get("LANGSTASH_ENABLED", "")
    debug(f"LANGSTASH_ENABLED={langstash_enabled}, LANGSTASH_URL={langstash_url}")


def main() -> int:
    start = time.time()
    debug("Hook started")
    _log_network_info()

    if not _HAS_DELIVER:
        debug("langstash_deliver not available; exiting.")
        return 0

    payload = read_hook_payload()
    if not payload:
        warn("empty or invalid hook payload on stdin")
        return 0

    ctx = extract_hook_context(payload)

    if not ctx.session_id or not ctx.transcript_path:
        warn(f"missing required fields: session_id={ctx.session_id}, transcript_path={ctx.transcript_path}")
        return 0

    if not ctx.transcript_path.exists():
        warn(f"transcript file does not exist: {ctx.transcript_path}")
        return 0

    user_id = resolve_user_id(ctx)
    tags_raw = _opt("LANGFUSE_TAGS") or "qoderwork"
    tags = [t.strip() for t in tags_raw.split(",") if t.strip()]

    # Use transcript directory for state persistence — QoderWork hooks run
    # inside a Linux VM where ~/.qoderwork/state/ is ephemeral. The transcript
    # directory is on a host-mounted volume that survives VM restarts.
    per_session_state_file = ctx.transcript_path.parent / ".langfuse_state.json"
    per_session_lock_file = ctx.transcript_path.parent / ".langfuse_state.lock"

    try:
        with FileLock(per_session_lock_file):
            state = {}
            try:
                if per_session_state_file.exists():
                    state = json.loads(per_session_state_file.read_text(encoding="utf-8"))
            except Exception:
                pass
            key = state_key(ctx.session_id, str(ctx.transcript_path))
            ss = load_session_state(state, key)

            def _save():
                write_session_state(state, key, ss)
                try:
                    tmp = per_session_state_file.with_suffix(".tmp")
                    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
                    os.replace(tmp, per_session_state_file)
                except Exception as e:
                    debug(f"save per-session state failed: {e}")

            msgs, ss = read_new_jsonl(ctx.transcript_path, ss)
            if not msgs:
                _save()
                return 0

            turns = build_turns(msgs)
            if not turns:
                _save()
                return 0

            emitted = 0
            for t in turns:
                emitted += 1
                turn_num = ss.turn_count + emitted

                try:
                    otlp_json = build_otlp_json(ctx, turn_num, t, user_id=user_id, tags=tags)
                    deliver_trace(otlp_json)
                except Exception as e:
                    debug(f"build/deliver failed: {e}")

            ss.turn_count += emitted
            _save()

        dur = time.time() - start
        info(f"Processed {emitted} turns in {dur:.2f}s (session={ctx.session_id})")
        return 0

    except TimeoutError as e:
        debug(f"lock timeout, skipping: {e}")
        return 0

    except Exception as e:
        debug(f"Unexpected failure: {e}")
        return 0

if __name__ == "__main__":
    sys.exit(main())
