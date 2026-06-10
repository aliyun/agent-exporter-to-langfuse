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

# --- Colors ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }

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
        hook.get('type') == 'command' and 'langfuse-entrypoint.sh' in hook.get('command', '')
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

# --- 4. Remove LaunchAgent (macOS only) ---
LAUNCH_AGENT_PLIST="$HOME/Library/LaunchAgents/com.codex.langfuse-env.plist"

if [ "$(uname)" = "Darwin" ] && [ -f "$LAUNCH_AGENT_PLIST" ]; then
    launchctl unload "$LAUNCH_AGENT_PLIST" 2>/dev/null || true
    rm -f "$LAUNCH_AGENT_PLIST"
    info "Removed LaunchAgent: $LAUNCH_AGENT_PLIST"
fi

echo ""
info "Uninstallation complete!"
echo ""
echo "  Restart Codex to apply changes."
