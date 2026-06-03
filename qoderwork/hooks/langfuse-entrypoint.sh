#!/bin/bash
cd "$(dirname "$0")"

[ -f "$HOME/.qoderwork/langfuse.env" ] && . "$HOME/.qoderwork/langfuse.env"

if ! .venv/bin/python -c "" 2>/dev/null; then
    rm -rf .venv
    python3 -m venv .venv
    .venv/bin/pip install -q langfuse 2>/dev/null
fi

exec .venv/bin/python langfuse_hook.py
