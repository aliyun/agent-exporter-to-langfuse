#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Source env file — try hook directory first (works in both VM and host),
# then $HOME-based path as fallback
for _env in \
    "$SCRIPT_DIR/langfuse.env" \
    "$HOME/.config/agent-exporter-to-langfuse/qoderwork.env"; do
    [ -f "$_env" ] && . "$_env" && break
done

if ! .venv/bin/python -c "" 2>/dev/null; then
    rm -rf .venv
    python3 -m venv .venv
    .venv/bin/pip install -q langfuse 2>/dev/null
fi

exec .venv/bin/python langfuse_hook.py
