#!/bin/bash
ENV_FILE="$HOME/.agent-exporter-to-langfuse/config/qoder.env"
[ -f "$ENV_FILE" ] && . "$ENV_FILE"
cd "$(dirname "$0")"
uv pip install -q "$HOME/.agent-exporter-to-langfuse/hooks/langstash-deliver/python" 2>/dev/null
exec uv run python langfuse_hook.py
