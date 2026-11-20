"""Shared builders and OTLP parsing helpers for the qoder-cn hook test suite.

Transcript rows follow the qoder-cn-family JSONL layout (isomorphic to the
qoder Desktop ``~/.qoder/projects/<dir>/<session>.jsonl`` transcripts the hook
is forked from): message rows carry a ``type`` of ``user`` / ``assistant`` /
``attachment`` with ``message.content[]`` blocks (text / tool_use /
tool_result), plus an ISO-8601 ``timestamp``.

The SharedClientCache SQLite DB fixture mirrors the ``chat_message`` table the
hook's ``query_session_tokens()`` reads (session_id, role, gmt_create epoch-ms,
token_info JSON, model_info JSON).
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


# ----------------- transcript row builders -----------------

def iso_ms(ts: str) -> int:
    """Epoch milliseconds for an ISO-8601 'Z' timestamp (DB gmt_create)."""
    return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)


def text_block(text: str) -> Dict[str, Any]:
    return {"type": "text", "text": text}


def tool_use_block(tool_id: str, name: str, tool_input: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input or {"cmd": "ls"}}


def user_row(text: str, ts: str) -> Dict[str, Any]:
    return {
        "type": "user",
        "message": {"role": "user", "content": [text_block(text)]},
        "timestamp": ts,
    }


def assistant_row(
    blocks: List[Dict[str, Any]],
    msg_id: str,
    ts: str,
    model: Optional[str] = None,
    usage: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    message: Dict[str, Any] = {"id": msg_id, "role": "assistant", "content": blocks}
    if model is not None:
        message["model"] = model
    if usage is not None:
        message["usage"] = usage
    return {"type": "assistant", "message": message, "timestamp": ts}


def tool_result_row(tool_use_id: str, content: str, ts: str) -> Dict[str, Any]:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": content}],
        },
        "timestamp": ts,
    }


def attachment_row(name: str, ts: str) -> Dict[str, Any]:
    """Non-message transcript row (type=attachment, no user/assistant role).

    The hook must tolerate it without treating it as a turn boundary.
    """
    return {
        "type": "attachment",
        "message": {"content": [text_block(f"attachment: {name}")]},
        "timestamp": ts,
    }


def write_transcript(path: Path, rows: List[Dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def append_transcript_rows(path: Path, rows: List[Dict[str, Any]]) -> Path:
    with open(path, "a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def stop_payload(session_id: str, transcript_path: Path) -> Dict[str, Any]:
    """qoder-cn Stop hook event payload (fields the hook consumes)."""
    return {
        "session_id": session_id,
        "transcript_path": str(transcript_path),
        "hook_event_name": "Stop",
        "cwd": "/tmp",
    }


# ----------------- SharedClientCache chat_message DB -----------------

def create_chat_db(db_path: Path, rows: List[Dict[str, Any]]) -> Path:
    """Create a SharedClientCache-shaped chat_message SQLite DB.

    Each row: session_id, role (default 'assistant'), token_info dict,
    model_info dict, gmt_create epoch-ms int — the columns the hook's
    ``query_session_tokens()`` SELECTs.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE chat_message (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                role TEXT,
                token_info TEXT,
                model_info TEXT,
                gmt_create INTEGER
            )
            """
        )
        for r in rows:
            token_info = json.dumps(r["token_info"]) if r.get("token_info") is not None else None
            model_info = json.dumps(r["model_info"]) if r.get("model_info") is not None else None
            conn.execute(
                "INSERT INTO chat_message (session_id, role, token_info, model_info, gmt_create)"
                " VALUES (?, ?, ?, ?, ?)",
                (r["session_id"], r.get("role", "assistant"), token_info, model_info, int(r["gmt_create"])),
            )
        conn.commit()
    finally:
        conn.close()
    return db_path


def db_row(session_id: str, ts: str, input_tokens: int, output_tokens: int,
           model_key: str, cached_tokens: Optional[int] = None) -> Dict[str, Any]:
    """One assistant chat_message row with token_info/model_info for `ts`."""
    token_info: Dict[str, Any] = {"prompt_tokens": input_tokens, "completion_tokens": output_tokens}
    if cached_tokens is not None:
        token_info["cached_tokens"] = cached_tokens
    return {
        "session_id": session_id,
        "role": "assistant",
        "token_info": token_info,
        "model_info": {"model_key": model_key},
        "gmt_create": iso_ms(ts),
    }


# ----------------- OTLP JSON parsing -----------------

def get_spans(otlp_json: Dict[str, Any]) -> List[Dict[str, Any]]:
    return otlp_json["resourceSpans"][0]["scopeSpans"][0]["spans"]


def get_attr(span: Dict[str, Any], key: str) -> Optional[Dict[str, Any]]:
    for attr in span.get("attributes", []):
        if attr["key"] == key:
            return attr["value"]
    return None


def attr_str(span: Dict[str, Any], key: str) -> Optional[str]:
    value = get_attr(span, key)
    return value.get("stringValue") if isinstance(value, dict) else None


def root_span(otlp_json: Dict[str, Any]) -> Dict[str, Any]:
    roots = [s for s in get_spans(otlp_json) if "parentSpanId" not in s]
    assert len(roots) == 1, (
        "expected exactly one root span (one turn trace) per delivered payload, "
        f"got {len(roots)}: {[s['name'] for s in roots]}"
    )
    return roots[0]


def spans_of_type(otlp_json: Dict[str, Any], obs_type: str) -> List[Dict[str, Any]]:
    return [s for s in get_spans(otlp_json) if attr_str(s, "langfuse.observation.type") == obs_type]


def trace_tags(otlp_json: Dict[str, Any]) -> List[str]:
    raw = attr_str(root_span(otlp_json), "langfuse.trace.tags")
    assert raw is not None, "root span must carry langfuse.trace.tags"
    return json.loads(raw)
