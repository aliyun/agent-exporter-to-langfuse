#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Source env file — try hook directory first (works in both VM and host),
# then $HOME-based path as fallback
for _env in \
    "$SCRIPT_DIR/langfuse.env" \
    "$HOME/.agent-exporter-to-langfuse/config/qoderwork.env"; do
    [ -f "$_env" ] && . "$_env" && break
done

if [ "$(uname)" = "Darwin" ]; then
    exec uv run python langfuse_hook.py
else
    # uv not available in VM, use pre-installed venv
    VENV_DIR="$SCRIPT_DIR/.venv-linux"
    if ! "$VENV_DIR/bin/python" -c "" 2>/dev/null; then
        rm -rf "$VENV_DIR"
        python3 -m venv "$VENV_DIR"
        "$VENV_DIR/bin/pip" install -q langfuse 2>/dev/null
    fi
    exec "$VENV_DIR/bin/python" langfuse_hook.py
fi
