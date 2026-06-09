"""
Build Trace Schema v2 JSON from hook Turn data.
"""

import uuid
from datetime import datetime
from typing import Any, Optional, TypedDict


class Usage(TypedDict, total=False):
    input: int
    output: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int


def build_trace_json(
    *,
    source: str,
    session_id: str,
    user_id: Optional[str],
    tags: list[str],
    trace_name: str,
    start_time: Optional[datetime],
    end_time: Optional[datetime],
    user_input: Any,
    assistant_output: Any,
    metadata: dict[str, Any],
    generations: list[dict[str, Any]],
    spans: list[dict[str, Any]],
) -> dict[str, Any]:
    def _ts(dt: Optional[datetime]) -> str:
        if dt is None:
            return ""
        return dt.isoformat()

    result: dict[str, Any] = {
        "schema_version": "2",
        "id": str(uuid.uuid4()),
        "source": source,
        "session_id": session_id,
        "trace": {
            "name": trace_name,
            "start_time": _ts(start_time),
            "end_time": _ts(end_time),
            "input": user_input,
            "output": assistant_output,
            "metadata": metadata,
        },
        "generations": generations,
        "spans": spans,
    }
    if user_id:
        result["user_id"] = user_id
    if tags:
        result["tags"] = tags
    return result


def build_generation(
    *,
    name: str,
    model: str,
    start_time: Optional[datetime],
    end_time: Optional[datetime],
    gen_input: Any = None,
    gen_output: Any = None,
    usage: Optional[Usage] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    def _ts(dt: Optional[datetime]) -> str:
        if dt is None:
            return ""
        return dt.isoformat()

    gen: dict[str, Any] = {
        "name": name,
        "model": model or "unknown",
        "start_time": _ts(start_time),
        "end_time": _ts(end_time),
        "output": gen_output or {},
    }
    if gen_input is not None:
        gen["input"] = gen_input
    if usage:
        gen["usage"] = {k: v for k, v in usage.items() if v}
    if metadata:
        gen["metadata"] = metadata
    return gen


def build_span(
    *,
    name: str,
    generation_index: int,
    start_time: Optional[datetime],
    end_time: Optional[datetime],
    span_input: Any = None,
    span_output: Any = None,
    metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    def _ts(dt: Optional[datetime]) -> str:
        if dt is None:
            return ""
        return dt.isoformat()

    span: dict[str, Any] = {
        "name": name,
        "generation_index": generation_index,
        "start_time": _ts(start_time),
        "end_time": _ts(end_time),
    }
    if span_input is not None:
        span["input"] = span_input
    if span_output is not None:
        span["output"] = span_output
    if metadata:
        span["metadata"] = metadata
    return span
