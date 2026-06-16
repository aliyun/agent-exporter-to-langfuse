#!/usr/bin/env bash
set -euo pipefail

# Codex Langfuse Hook Uninstaller
# Removes hook files from ~/.codex/hooks/langfuse/ and unregisters Stop hook from ~/.codex/hooks.json

CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
HOOKS_DIR="$CODEX_HOME/hooks"
LANGFUSE_HOOK_DIR="$HOOKS_DIR/langfuse"
CODEX_HOOKS_JSON="$CODEX_HOME/hooks.json"

# Shared config
LANGFUSE_PROFILE_DIR="$HOME/.agent-exporter-to-langfuse/config"
LANGFUSE_ENV_FILE="$LANGFUSE_PROFILE_DIR/codex.env"

# Shell profile
if [ -n "${ZSH_VERSION:-}" ] || [ "$(basename "${SHELL:-}")" = "zsh" ]; then
    SHELL_RC="$HOME/.zshenv"
else
    SHELL_RC="$HOME/.profile"
fi

# --- Colors ---
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }

# --- Parse arguments ---
while [[ $# -gt 0 ]]; do
    case "$1" in
        *) shift ;;
    esac
done

# --- 1. Remove hook files ---
if [ -d "$LANGFUSE_HOOK_DIR" ]; then
    rm -rf "$LANGFUSE_HOOK_DIR"
    info "Removed hook directory: $LANGFUSE_HOOK_DIR"
else
    info "Hook directory not found: $LANGFUSE_HOOK_DIR (skipping)"
fi

# --- 2. Remove Stop hook from ~/.codex/hooks.json ---
if [ -f "$CODEX_HOOKS_JSON" ]; then
    python3 -c "
import json, sys, os

hooks_json_path = sys.argv[1]

# Load hooks.json
with open(hooks_json_path, 'r') as f:
    hooks_data = json.load(f)

# Check if Stop hooks exist
if 'hooks' not in hooks_data or 'Stop' not in hooks_data['hooks']:
    print('no_stop_hooks')
    sys.exit(0)

# Remove langfuse hook entries
original_count = len(hooks_data['hooks']['Stop'])
hooks_data['hooks']['Stop'] = [
    matcher for matcher in hooks_data['hooks']['Stop']
    if not any(
        hook.get('type') == 'command' and 'langfuse' in hook.get('command', '')
        for hook in matcher.get('hooks', [])
    )
]

new_count = len(hooks_data['hooks']['Stop'])

if new_count < original_count:
    # Write back
    with open(hooks_json_path, 'w') as f:
        json.dump(hooks_data, f, indent=2)
        f.write('\n')
    print('removed')
else:
    print('not_found')
" "$CODEX_HOOKS_JSON" | while IFS= read -r status; do
    case "$status" in
        removed) info "Removed Stop hook from $CODEX_HOOKS_JSON" ;;
        not_found) info "Stop hook not found in $CODEX_HOOKS_JSON (already removed)" ;;
        no_stop_hooks) info "No Stop hooks configured in $CODEX_HOOKS_JSON" ;;
    esac
done
else
    info "hooks.json not found: $CODEX_HOOKS_JSON (skipping)"
fi

# --- 3. Remove env file ---
if [ -f "$LANGFUSE_ENV_FILE" ]; then
    rm -f "$LANGFUSE_ENV_FILE"
    info "Removed env file: $LANGFUSE_ENV_FILE"
else
    info "Env file not found: $LANGFUSE_ENV_FILE (skipping)"
fi

# --- 4. Clean up profile.d directory if empty ---
if [ -d "$LANGFUSE_PROFILE_DIR" ] && [ -z "$(ls -A "$LANGFUSE_PROFILE_DIR" 2>/dev/null)" ]; then
    rmdir "$LANGFUSE_PROFILE_DIR"
    info "Removed empty profile directory: $LANGFUSE_PROFILE_DIR"

    # Remove loader line from shell profile only when no agents remain
    if [ -f "$SHELL_RC" ] && grep -qF "agent-exporter-to-langfuse" "$SHELL_RC" 2>/dev/null; then
        python3 -c "
import sys
lines = open(sys.argv[1]).readlines()
out = []
skip_next = False
for line in lines:
    if '# Agent Langfuse Exporters' in line:
        skip_next = True
        if out and out[-1].strip() == '':
            out.pop()
        continue
    if skip_next and 'agent-exporter-to-langfuse' in line:
        skip_next = False
        continue
    skip_next = False
    out.append(line)
open(sys.argv[1], 'w').writelines(out)
" "$SHELL_RC"
        info "Removed loader line from $SHELL_RC"
    fi
fi

# --- 5. Remove state files ---
STATE_DIR="$CODEX_HOME/state"
removed_state=false
for f in "$STATE_DIR/langfuse_hook.log"* "$STATE_DIR/langfuse_state.json" "$STATE_DIR/langfuse_state.lock"; do
    if [ -f "$f" ]; then
        rm -f "$f"
        removed_state=true
    fi
done
if [ "$removed_state" = true ]; then
    info "Removed Langfuse state/log files from $STATE_DIR"
fi

echo ""
info "Uninstallation complete!"
echo ""
echo "  Restart Codex to apply changes."
