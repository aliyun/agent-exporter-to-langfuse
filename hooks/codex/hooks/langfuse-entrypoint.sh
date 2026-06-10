#!/bin/bash
ENV_FILE="$HOME/.agent-exporter-to-langfuse/config/codex.env"
[ -f "$ENV_FILE" ] && . "$ENV_FILE"
cd "$(dirname "$0")"
exec uv run python langfuse_hook.py
