#!/bin/bash
ENV_FILE="$HOME/.config/agent-exporter-to-langfuse/qoder.env"
[ -f "$ENV_FILE" ] && . "$ENV_FILE"
cd "$(dirname "$0")"
exec uv run python langfuse_hook.py
