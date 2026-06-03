#!/bin/bash
cd "$(dirname "$0")"
exec uv run python langfuse_hook.py
