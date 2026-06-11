#!/usr/bin/env python3
"""
Qoder -> Langfuse hook

"""

import json
import logging
import os
import sys
import threading
import time
import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
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

# --- Qoder SQLite DB for token/model enrichment ---
def _find_qoder_db() -> Optional[Path]:
    """Locate the Qoder SharedClientCache SQLite DB (Desktop / IDE)."""
    candidates = []
    home = Path.home()
    if sys.platform == "darwin":
        candidates.append(home / "Library" / "Application Support" / "Qoder" / "SharedClientCache" / "cache" / "db" / "local.db")
    else:
        candidates.append(home / ".local" / "share" / "Qoder" / "SharedClientCache" / "cache" / "db" / "local.db")
    candidates.append(home / ".qoder" / "shared_client" / "cache" / "db" / "local.db")
    for p in candidates:
        if p.exists():
            return p
    return None

@dataclass
class TokenInfo:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    model_key: str = ""
    gmt_create_ms: int = 0

def query_session_tokens(session_id: str) -> List[TokenInfo]:
    """Query per-assistant-message token usage from the Qoder SQLite DB.

    Returns rows ordered by gmt_create ASC so they can be matched to
    transcript assistant messages by timestamp proximity.
    """
    db_path = _find_qoder_db()
    if not db_path:
        return []
    try:
        import sqlite3
        conn = sqlite3.connect(str(db_path), timeout=2)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT
                gmt_create,
                json_extract(token_info, '$.prompt_tokens')     AS input_tokens,
                json_extract(token_info, '$.completion_tokens') AS output_tokens,
                json_extract(token_info, '$.cached_tokens')     AS cached_tokens,
                CASE
                    WHEN model_info IS NOT NULL AND model_info != ''
                        THEN COALESCE(json_extract(model_info, '$.model_key'), '')
                    ELSE ''
                END AS model_key
            FROM chat_message
            WHERE session_id = ?
              AND role = 'assistant'
              AND token_info IS NOT NULL AND token_info != ''
            ORDER BY gmt_create ASC
            """,
            (session_id,),
        ).fetchall()
        conn.close()
        return [
            TokenInfo(
                input_tokens=int(r["input_tokens"] or 0),
                output_tokens=int(r["output_tokens"] or 0),
                cached_tokens=int(r["cached_tokens"] or 0),
                model_key=str(r["model_key"] or ""),
                gmt_create_ms=int(r["gmt_create"] or 0),
            )
            for r in rows
        ]
    except Exception:
        return []

def match_db_token(db_tokens: List[TokenInfo], ts: Optional[datetime], used: set) -> Optional[TokenInfo]:
    """Find the best DB token record matching a transcript timestamp.

    Uses nearest-timestamp within a 5-second window. Each DB record is
    used at most once (tracked via `used` set of indices).
    """
    if not db_tokens or ts is None:
        return None
    ts_ms = int(ts.timestamp() * 1000)
    best_idx = -1
    best_delta = 5000  # max 5s tolerance
    for i, tok in enumerate(db_tokens):
        if i in used:
            continue
        delta = abs(tok.gmt_create_ms - ts_ms)
        if delta < best_delta:
            best_delta = delta
            best_idx = i
    if best_idx >= 0:
        used.add(best_idx)
        return db_tokens[best_idx]
    return None

# --- Paths ---
STATE_DIR = Path.home() / ".qoder" / "state"
LOG_FILE = STATE_DIR / "langfuse_hook.log"
STATE_FILE = STATE_DIR / "langfuse_state.json"
LOCK_FILE = STATE_DIR / "langfuse_state.lock"

def _opt(name: str) -> str:
    """Read a configuration value from environment variables."""
    return os.environ.get(name) or ""

DEBUG = (_opt("LANGFUSE_DEBUG") or "true").lower() != "false"
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
        lg = logging.getLogger("qoder_langfuse_hook")
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
            # No fcntl available (e.g. Windows) — proceed without lock.
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
            # __exit__ is not called when __enter__ raises — close the fh
            # we just opened so it doesn't leak.
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
        # Drop session entries older than 30 days to keep the file bounded.
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
    # stable key even if session_id collides
    raw = f"{session_id}::{transcript_path}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

# ----------------- Hook payload -----------------
def read_hook_payload() -> Dict[str, Any]:
    """
    Qoder hooks pass a JSON payload on stdin.
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
    """Extract session, transcript, and context from the Qoder Stop hook payload.

    CLI payload fields:
      session_id, transcript_path, cwd, hook_event_name,
      permission_mode, stop_hook_active, last_assistant_message

    Desktop payload adds:
      extra.branch, extra.email, extra.repo,
      extra.user.{email, name, uid, org_id, org_name}
    """
    ctx = HookContext()
    ctx.session_id = payload.get("session_id")
    ctx.hook_event = payload.get("hook_event_name")
    ctx.cwd = payload.get("cwd")

    if not ctx.session_id:
        warn("payload missing session_id")
    if ctx.hook_event and ctx.hook_event not in ("Stop", "SubagentStop"):
        warn(f"unexpected hook_event_name: {ctx.hook_event}")

    # SubagentStop: use agent_transcript_path (subagent's own transcript),
    # NOT transcript_path (main agent's transcript — reading it would consume
    # the main agent's offset and cause the Stop hook to miss data).
    # Desktop SubagentStop may not provide agent_transcript_path — skip gracefully.
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
    # Qoder transcript lines commonly have type=user/assistant OR message.role
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
    """Extract model from JSONL message. Returns the raw value (e.g. 'lite')."""
    m = msg.get("message")
    if isinstance(m, dict):
        v = m.get("model")
        if v and isinstance(v, str):
            return v
    return ""

def get_usage(msg: Dict[str, Any]) -> Optional["Usage"]:
    """Extract token usage from an assistant message, if present."""
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
    """Parse a Qoder jsonl row timestamp (ISO 8601 with trailing Z)."""
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
    """
    Reads only new bytes since ss.offset. Keeps ss.buffer for partial last line.
    Returns parsed JSON lines (best-effort) and updated state.
    """
    if not transcript_path.exists():
        return [], ss

    try:
        file_size = transcript_path.stat().st_size
        if file_size < ss.offset:
            # Transcript was rotated or truncated — restart from the beginning.
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
    # last element may be incomplete
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
    """Merge content blocks from a new row into an existing assistant message.

    Qoder writes each content block (thinking, text, tool_use) as a separate
    JSONL row sharing the same message.id.  We accumulate all blocks into a
    single message so that tool_use blocks are not lost.
    """
    merged = dict(existing)
    old_content = get_content(merged)
    new_content = get_content(new_row)

    if not isinstance(old_content, list):
        old_content = [old_content] if old_content else []
    if not isinstance(new_content, list):
        new_content = [new_content] if new_content else []

    # Append new blocks
    combined = list(old_content) + list(new_content)
    if "message" in merged and isinstance(merged["message"], dict):
        merged["message"] = dict(merged["message"])
        merged["message"]["content"] = combined
        # Carry over usage, model, stop_reason from later rows (typically
        # the last row of a message.id group has stop_reason + usage).
        new_msg = new_row.get("message")
        if isinstance(new_msg, dict):
            for key in ("usage", "model", "stop_reason"):
                val = new_msg.get(key)
                if val is not None:
                    merged["message"][key] = val
    # Keep first row's timestamp as start; track latest as end_timestamp
    if "timestamp" in new_row:
        merged["end_timestamp"] = new_row["timestamp"]
    return merged

def build_turns(messages: List[Dict[str, Any]]) -> List[Turn]:
    """
    Groups incremental transcript rows into turns:
    user (non-tool-result) -> assistant messages -> (tool_result rows, possibly interleaved)

    Qoder writes each content block as a separate JSONL row with the same
    message.id, so rows sharing an id are MERGED (content blocks accumulated)
    rather than overwritten.
    """
    turns: List[Turn] = []
    current_user: Optional[Dict[str, Any]] = None

    # assistant messages for current turn:
    assistant_order: List[str] = []             # message ids in order of first appearance (or synthetic)
    assistant_latest: Dict[str, Dict[str, Any]] = {}  # id -> merged msg
    noid_group: int = 0                         # counter for grouping consecutive no-id assistant rows
    last_was_noid_assistant: bool = False        # track if previous row was a no-id assistant

    tool_results_by_id: Dict[str, Any] = {}     # tool_use_id -> content

    def flush_turn():
        nonlocal current_user, assistant_order, assistant_latest, tool_results_by_id, turns
        if current_user is None:
            return
        if not assistant_latest:
            warn("turn has user message but no assistant messages, skipping")
            return
        assistants = [assistant_latest[mid] for mid in assistant_order if mid in assistant_latest]
        # Check for tool_use blocks that have no matching tool_result
        for am in assistants:
            for tu in iter_tool_uses(get_content(am)):
                tid = tu.get("id")
                if tid and str(tid) not in tool_results_by_id:
                    warn(f"tool_use {tu.get('name')}({tid}) has no matching tool_result")
        turns.append(Turn(user_msg=current_user, assistant_msgs=assistants, tool_results_by_id=dict(tool_results_by_id)))

    for msg in messages:
        role = get_role(msg)

        # tool_result rows show up as role=user with content blocks of type tool_result
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
            # new user message -> finalize previous turn
            flush_turn()

            # start a new turn
            current_user = msg
            assistant_order = []
            assistant_latest = {}
            noid_group = 0
            tool_results_by_id = {}
            continue

        if role == "assistant":
            if current_user is None:
                # ignore assistant rows until we see a user message
                continue

            mid = get_message_id(msg)
            if mid is None:
                # No message.id (Desktop): merge consecutive rows into same group
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

        # Only reset noid merge on role boundaries (user, tool_result),
        # not on progress/session_meta/other non-message rows.
        # last_was_noid_assistant is already reset in the user/tool_result blocks above.

        # ignore unknown rows

    # flush last
    flush_turn()
    return turns

# ----------------- Langfuse emit -----------------
def _to_ns(ts: Optional[datetime]) -> Optional[int]:
    """Convert a datetime to OTel-style nanoseconds since epoch."""
    if ts is None:
        return None
    return int(ts.timestamp() * 1_000_000_000)


def _start_backdated(langfuse: Langfuse, *, name: str, as_type: str,
                     start_time: Optional[datetime],
                     parent_otel_span: Any = None,
                     **obs_kwargs: Any) -> Any:
    """Create a Langfuse observation with an explicit OTel start_time.

    Bypasses langfuse.start_observation() (which has no start_time kwarg in
    SDK 4.x) by talking to the underlying OTel tracer directly and then
    wrapping the resulting span with the Langfuse observation type.

    Depends on SDK 4.x internals: langfuse._otel_tracer and
    langfuse._create_observation_from_otel_span. If a future SDK version
    renames or removes these, raise a clear error instead of letting an
    AttributeError get swallowed by the broad emit_turn handler.
    """
    if not hasattr(langfuse, "_otel_tracer") or not hasattr(langfuse, "_create_observation_from_otel_span"):
        try:
            sdk_version = getattr(__import__("langfuse"), "__version__", "unknown")
        except Exception:
            sdk_version = "unknown"
        raise RuntimeError(
            f"Langfuse SDK {sdk_version} is missing _otel_tracer or "
            f"_create_observation_from_otel_span. This hook targets SDK 4.x; "
            f"pin with `pip install \"langfuse>=4.0,<5\"` or update the hook script."
        )
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


def emit_turn(langfuse: Langfuse, ctx: HookContext, turn_num: int, turn: Turn, user_id: Optional[str] = None, tags: Optional[List[str]] = None, db_tokens: Optional[List[TokenInfo]] = None, db_used: Optional[set] = None) -> None:
    user_text_raw = extract_text(get_content(turn.user_msg))
    user_text, user_text_meta = truncate_text(user_text_raw)

    last_assistant = turn.assistant_msgs[-1]
    final_assistant_text, _ = truncate_text(extract_text(get_content(last_assistant)))

    user_ts = parse_ts(turn.user_msg)
    last_assistant_ts = parse_ts({"timestamp": last_assistant.get("end_timestamp")}) if last_assistant.get("end_timestamp") else parse_ts(last_assistant)
    # Pick a turn end_time: latest among final assistant message or any tool result
    candidate_end_ts = [t for t in [last_assistant_ts] if t is not None]
    for tr in turn.tool_results_by_id.values():
        t = parse_ts(tr)
        if t is not None:
            candidate_end_ts.append(t)
    turn_end_ts = max(candidate_end_ts) if candidate_end_ts else None

    is_subagent = ctx.hook_event == "SubagentStop"
    trace_label = f"Qoder - Subagent Turn {turn_num}" if is_subagent else f"Qoder - Turn {turn_num}"

    pa_kwargs: Dict[str, Any] = {
        "session_id": ctx.session_id,
        "trace_name": trace_label,
        "tags": tags or ["qoder"],
    }
    if user_id:
        pa_kwargs["user_id"] = user_id

    trace_metadata: Dict[str, Any] = {
        "source": "qoder-subagent" if is_subagent else "qoder",
        "session_id": ctx.session_id,
        "turn_number": turn_num,
        "transcript_path": str(ctx.transcript_path),
        "user_text": user_text_meta,
        "assistant_message_count": len(turn.assistant_msgs),
    }
    if ctx.agent_id:
        trace_metadata["agent_id"] = ctx.agent_id
    if ctx.agent_type:
        trace_metadata["agent_type"] = ctx.agent_type
    if ctx.cwd:
        trace_metadata["cwd"] = ctx.cwd
    if ctx.repo:
        trace_metadata["repo"] = ctx.repo
    if ctx.branch:
        trace_metadata["branch"] = ctx.branch
    if ctx.email:
        trace_metadata["email"] = ctx.email
    if ctx.org_name:
        trace_metadata["org_name"] = ctx.org_name
    if ctx.user_uid:
        trace_metadata["user_uid"] = ctx.user_uid

    with propagate_attributes(**pa_kwargs):
        trace_span = _start_backdated(
            langfuse,
            name=trace_label,
            as_type="span",
            start_time=user_ts,
            input={"role": "user", "content": user_text},
            metadata=trace_metadata,
        )
        parent_otel_span = trace_span._otel_span

        # Iterate each assistant message: emit generation, then its tool_use children.
        # prev_ts = the moment the next generation could have started (= when the previous
        # batch of tool results all returned, or the original user message timestamp).
        prev_ts = user_ts
        prev_tool_results: List[Dict[str, Any]] = []  # populated after each batch, surfaced as next gen's input

        for idx, am in enumerate(turn.assistant_msgs):
            am_ts = parse_ts(am)
            am_end_ts = parse_ts({"timestamp": am.get("end_timestamp")}) if am.get("end_timestamp") else am_ts
            am_text_raw = extract_text(get_content(am))
            am_text, am_text_meta = truncate_text(am_text_raw)
            model = get_model(am)
            tool_uses = iter_tool_uses(get_content(am))

            # Build generation input: user message for first generation, otherwise tool results from
            # the prior batch (best partial reconstruction of the prompt context).
            if idx == 0:
                gen_input: Any = {"role": "user", "content": user_text}
            elif prev_tool_results:
                gen_input = {"role": "tool", "tool_results": prev_tool_results}
            else:
                gen_input = None

            # Build generation output: include both the text response and any tool calls the LLM
            # decided to make. Most assistant messages in tool-using turns are tool-call-only, so
            # without tool_calls in the output, the observation looks empty.
            gen_tool_calls = []
            for tu in tool_uses:
                tu_input = tu.get("input")
                if isinstance(tu_input, str):
                    tu_input_serialized, _ = truncate_text(tu_input)
                else:
                    tu_input_serialized = tu_input
                gen_tool_calls.append({
                    "id": tu.get("id"),
                    "name": tu.get("name"),
                    "input": tu_input_serialized,
                })

            gen_output: Dict[str, Any] = {"role": "assistant"}
            if am_text:
                gen_output["content"] = am_text
            if gen_tool_calls:
                gen_output["tool_calls"] = gen_tool_calls

            # Enrich from DB: match by timestamp proximity
            db_token = match_db_token(db_tokens or [], am_ts, db_used) if db_used is not None else None
            if db_token and db_token.model_key and not model:
                model = db_token.model_key

            if not model:
                warn(f"turn {turn_num} generation {idx+1}: no model in transcript or DB")
            if not am_ts:
                warn(f"turn {turn_num} generation {idx+1}: missing timestamp")
            if not am_text and not gen_tool_calls:
                warn(f"turn {turn_num} generation {idx+1}: empty output (no text, no tool_calls)")

            gen_meta: Dict[str, Any] = {
                "assistant_index": idx,
                "assistant_text": am_text_meta,
                "tool_count": len(tool_uses),
            }
            if db_token:
                gen_meta["db_model_key"] = db_token.model_key

            gen_kwargs: Dict[str, Any] = dict(
                model=model or "unknown",
                input=gen_input,
                output=gen_output,
                metadata=gen_meta,
            )
            usage_details = get_usage(am)
            if usage_details is None and db_token:
                usage_details = {"input": db_token.input_tokens, "output": db_token.output_tokens}
                if db_token.cached_tokens > 0:
                    usage_details["cache_read_input_tokens"] = db_token.cached_tokens
            if usage_details is not None:
                gen_kwargs["usage_details"] = usage_details

            gen_span = _start_backdated(
                langfuse,
                name=f"Qoder Generation {idx + 1}",
                as_type="generation",
                start_time=prev_ts or am_ts,
                parent_otel_span=parent_otel_span,
                **gen_kwargs,
            )

            # Tool observations: nested under this generation. Each starts when the assistant
            # emitted the tool_use (am_ts) and ends when its tool_result row arrived.
            batch_result_ts: List[datetime] = []
            batch_tool_results: List[Dict[str, Any]] = []
            for tu in tool_uses:
                tid = str(tu.get("id") or "")
                tname = tu.get("name") or "unknown"
                tinput_raw = tu.get("input") if isinstance(tu.get("input"), (dict, list, str, int, float, bool)) else {}
                if isinstance(tinput_raw, str):
                    tinput, tinput_meta = truncate_text(tinput_raw)
                else:
                    tinput, tinput_meta = tinput_raw, None

                tr_entry = turn.tool_results_by_id.get(tid) if tid else None
                if tr_entry:
                    out_raw = tr_entry.get("content")
                    out_str = out_raw if isinstance(out_raw, str) else json.dumps(out_raw, ensure_ascii=False)
                    out_trunc, out_meta = truncate_text(out_str)
                    tr_ts = parse_ts(tr_entry.get("timestamp"))
                else:
                    out_trunc, out_meta, tr_ts = None, None, None
                if tr_ts is not None:
                    batch_result_ts.append(tr_ts)

                tool_span = _start_backdated(
                    langfuse,
                    name=f"Tool: {tname}",
                    as_type="tool",
                    start_time=am_ts,
                    parent_otel_span=gen_span._otel_span,
                    input=tinput,
                    metadata={
                        "tool_name": tname,
                        "tool_id": tid,
                        "input_meta": tinput_meta,
                        "output_meta": out_meta,
                    },
                )
                tool_span.update(output=out_trunc)
                tool_span.end(end_time=_to_ns(tr_ts or am_ts))

                batch_tool_results.append({
                    "tool_use_id": tid,
                    "tool_name": tname,
                    "output": out_trunc,
                })

            # End the generation AFTER its tools so the timeline cleanly contains them.
            # Priority: latest tool_result ts > am_end_ts (last content block) > am_ts (first block)
            gen_end_ts = max(batch_result_ts) if batch_result_ts else (am_end_ts or am_ts)
            gen_span.end(end_time=_to_ns(gen_end_ts or am_ts or prev_ts))

            # Carry this batch's results into the next generation's input.
            prev_tool_results = batch_tool_results

            # Advance prev_ts: next generation can only start after this batch's tool results returned.
            if batch_result_ts:
                prev_ts = max(batch_result_ts)
            elif am_end_ts is not None:
                prev_ts = am_end_ts
            elif am_ts is not None:
                prev_ts = am_ts

        trace_span.update(output={"role": "assistant", "content": final_assistant_text})
        trace_span.end(end_time=_to_ns(turn_end_ts or last_assistant_ts or user_ts))

def resolve_user_id(ctx: HookContext) -> Optional[str]:
    """Priority: env var > payload extra.user.name > OS username."""
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

# ----------------- Trace Schema v2 builder -----------------
def _build_trace_v2(ctx: HookContext, turn_num: int, turn: Turn,
                    user_id: Optional[str], tags: List[str],
                    db_tokens: Optional[List[TokenInfo]] = None, db_used: Optional[set] = None) -> Dict[str, Any]:
    user_text, _ = truncate_text(extract_text(get_content(turn.user_msg)))
    last_assistant = turn.assistant_msgs[-1]
    final_text, _ = truncate_text(extract_text(get_content(last_assistant)))

    user_ts = parse_ts(turn.user_msg)
    last_ts = parse_ts({"timestamp": last_assistant.get("end_timestamp")}) if last_assistant.get("end_timestamp") else parse_ts(last_assistant)
    candidate_end = [t for t in [last_ts] if t is not None]
    for tr in turn.tool_results_by_id.values():
        t = parse_ts(tr)
        if t is not None:
            candidate_end.append(t)
    turn_end = max(candidate_end) if candidate_end else None

    is_subagent = ctx.hook_event == "SubagentStop"
    trace_label = f"Qoder - Subagent Turn {turn_num}" if is_subagent else f"Qoder - Turn {turn_num}"

    trace_meta: Dict[str, Any] = {
        "source": "qoder-subagent" if is_subagent else "qoder",
        "turn_number": turn_num, "is_subagent": is_subagent,
        "assistant_message_count": len(turn.assistant_msgs),
        "transcript_path": str(ctx.transcript_path),
    }
    for k, v in [("cwd", ctx.cwd), ("repo", ctx.repo), ("branch", ctx.branch),
                 ("email", ctx.email), ("org_name", ctx.org_name), ("user_uid", ctx.user_uid),
                 ("agent_id", ctx.agent_id), ("agent_type", ctx.agent_type)]:
        if v:
            trace_meta[k] = v

    generations: List[Dict[str, Any]] = []
    spans: List[Dict[str, Any]] = []
    prev_ts = user_ts

    for idx, am in enumerate(turn.assistant_msgs):
        am_ts = parse_ts(am)
        am_end_ts = parse_ts({"timestamp": am.get("end_timestamp")}) if am.get("end_timestamp") else am_ts
        am_text, _ = truncate_text(extract_text(get_content(am)))
        model = get_model(am)
        tool_uses = iter_tool_uses(get_content(am))

        gen_input: Any = {"role": "user", "content": user_text} if idx == 0 else None

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

        db_token = match_db_token(db_tokens or [], am_ts, db_used) if db_used is not None else None
        if db_token and db_token.model_key and not model:
            model = db_token.model_key

        usage = get_usage(am)
        if usage is None and db_token:
            usage = {"input": db_token.input_tokens, "output": db_token.output_tokens}
            if db_token.cached_tokens > 0:
                usage["cache_read_input_tokens"] = db_token.cached_tokens

        gen = build_generation(
            name=f"Qoder Generation {idx + 1}", model=model or "unknown",
            start_time=prev_ts or am_ts, end_time=am_end_ts or am_ts,
            gen_input=gen_input, gen_output=gen_output,
            usage=usage, metadata={"assistant_index": idx, "tool_count": len(tool_uses)},
        )
        generations.append(gen)

        batch_end: List[datetime] = []
        for tu in tool_uses:
            tid = str(tu.get("id") or "")
            tname = tu.get("name") or "unknown"
            tinput_raw = tu.get("input") if isinstance(tu.get("input"), (dict, list, str, int, float, bool)) else {}
            tinput = truncate_text(tinput_raw)[0] if isinstance(tinput_raw, str) else tinput_raw
            tr_entry = turn.tool_results_by_id.get(tid)
            out_trunc, tr_ts = None, None
            if tr_entry:
                out_raw = tr_entry.get("content")
                out_str = out_raw if isinstance(out_raw, str) else json.dumps(out_raw, ensure_ascii=False)
                out_trunc, _ = truncate_text(out_str)
                tr_ts = parse_ts(tr_entry.get("timestamp"))
            if tr_ts:
                batch_end.append(tr_ts)
            spans.append(build_span(
                name=f"Tool: {tname}", generation_index=idx,
                start_time=am_ts, end_time=tr_ts or am_ts,
                span_input=tinput, span_output=out_trunc,
                metadata={"tool_name": tname, "tool_id": tid},
            ))

        if batch_end:
            prev_ts = max(batch_end)
        elif am_end_ts:
            prev_ts = am_end_ts
        elif am_ts:
            prev_ts = am_ts

    return build_trace_json(
        source="qoder", session_id=ctx.session_id, user_id=user_id, tags=tags,
        trace_name=trace_label, start_time=user_ts, end_time=turn_end,
        user_input={"role": "user", "content": user_text},
        assistant_output={"role": "assistant", "content": final_text},
        metadata=trace_meta, generations=generations, spans=spans,
    )

# ----------------- Main -----------------
def main() -> int:
    start = time.time()
    debug("Hook started")

    public_key = _opt("LANGFUSE_PUBLIC_KEY")
    secret_key = _opt("LANGFUSE_SECRET_KEY")
    host = _opt("LANGFUSE_BASE_URL") or "https://us.cloud.langfuse.com"

    if not public_key or not secret_key:
        warn("LANGFUSE_PUBLIC_KEY or LANGFUSE_SECRET_KEY not set, skipping")
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
    tags_raw = _opt("LANGFUSE_TAGS") or "qoder"
    tags = [t.strip() for t in tags_raw.split(",") if t.strip()]

    langfuse = None
    try:
        langfuse = Langfuse(public_key=public_key, secret_key=secret_key, host=host)
    except Exception:
        return 0

    try:
        with FileLock(LOCK_FILE):
            state = load_state()
            key = state_key(ctx.session_id, str(ctx.transcript_path))
            ss = load_session_state(state, key)

            msgs, ss = read_new_jsonl(ctx.transcript_path, ss)
            if not msgs:
                write_session_state(state, key, ss)
                save_state(state)
                return 0

            turns = build_turns(msgs)
            if not turns:
                write_session_state(state, key, ss)
                save_state(state)
                return 0

            # Query DB for token/model enrichment (best-effort)
            db_tokens = query_session_tokens(ctx.session_id)
            if db_tokens:
                debug(f"DB enrichment: {len(db_tokens)} token records for session {ctx.session_id}")
            else:
                debug(f"No DB token data for session {ctx.session_id}")

            # emit turns — db_used tracks which DB records have been consumed
            emitted = 0
            db_used: set = set()
            for t in turns:
                emitted += 1
                turn_num = ss.turn_count + emitted

                if _HAS_DELIVER:
                    try:
                        trace_json = _build_trace_v2(ctx, turn_num, t, user_id=user_id, tags=tags,
                                                     db_tokens=db_tokens or None, db_used=db_used)
                    except Exception as e:
                        debug(f"_build_trace_v2 failed: {e}")
                        trace_json = None

                    def _direct_push(_tj, _t=t, _turn_num=turn_num):
                        emit_turn(langfuse, ctx, _turn_num, _t, user_id=user_id, tags=tags,
                                  db_tokens=db_tokens or None, db_used=db_used)
                        return True

                    if trace_json:
                        deliver_trace(trace_json, direct_push_fn=_direct_push)
                    else:
                        _direct_push(None)
                else:
                    try:
                        emit_turn(langfuse, ctx, turn_num, t, user_id=user_id, tags=tags,
                                  db_tokens=db_tokens or None, db_used=db_used)
                    except Exception as e:
                        info(f"emit_turn failed: {type(e).__name__}: {e}")

            ss.turn_count += emitted
            write_session_state(state, key, ss)
            save_state(state)

        dur = time.time() - start
        info(f"Processed {emitted} turns in {dur:.2f}s (session={ctx.session_id})")
        return 0

    except TimeoutError as e:
        debug(f"lock timeout, skipping: {e}")
        return 0

    except Exception as e:
        debug(f"Unexpected failure: {e}")
        return 0

    finally:
        # Cap flush+shutdown at 5s so a slow/unreachable Langfuse can't stall Qoder.
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
