#!/usr/bin/env bash
set -euo pipefail

# Cursor Langfuse Hook Uninstaller
# Removes hook entries (command containing 'langfuse') from ~/.cursor/hooks.json
# and deletes the bundle from ~/.cursor/hooks/langfuse/dist/.

CURSOR_HOME="${CURSOR_HOME:-$HOME/.cursor}"
LANGFUSE_HOOK_DIR="$CURSOR_HOME/hooks/langfuse"
CURSOR_HOOKS_JSON="$CURSOR_HOME/hooks.json"

# Shared config
LANGFUSE_PROFILE_DIR="$HOME/.agent-exporter-to-langfuse/config"
LANGFUSE_ENV_FILE="$LANGFUSE_PROFILE_DIR/cursor.env"

# --- Colors ---
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }

# --- Parse arguments ---
PURGE=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        --purge) PURGE=true; shift ;;
        *) shift ;;
    esac
done

# --- 1. Remove hook bundle ---
if [ -d "$LANGFUSE_HOOK_DIR" ]; then
    rm -rf "$LANGFUSE_HOOK_DIR"
    info "Removed hook directory: $LANGFUSE_HOOK_DIR"
else
    info "Hook directory not found: $LANGFUSE_HOOK_DIR (skipping)"
fi

# --- 2. Remove langfuse hook entries from ~/.cursor/hooks.json ---
if [ -f "$CURSOR_HOOKS_JSON" ]; then
    python3 -c "
import json, sys

hooks_json_path = sys.argv[1]

with open(hooks_json_path, 'r') as f:
    hooks_data = json.load(f)

if not isinstance(hooks_data, dict):
    print('invalid')
    sys.exit(0)

removed_count = 0
empty_events = []
for event in list(hooks_data.keys()):
    if not isinstance(hooks_data[event], list):
        continue
    original = len(hooks_data[event])
    hooks_data[event] = [
        entry for entry in hooks_data[event]
        if not (isinstance(entry, dict) and 'langfuse' in str(entry.get('command', '')))
    ]
    removed_count += original - len(hooks_data[event])
    if len(hooks_data[event]) == 0:
        empty_events.append(event)

# Remove empty event arrays left after removal
for event in empty_events:
    del hooks_data[event]

if removed_count > 0:
    with open(hooks_json_path, 'w') as f:
        json.dump(hooks_data, f, indent=2)
        f.write('\n')
    print('removed:%d' % removed_count)
else:
    print('not_found')
" "$CURSOR_HOOKS_JSON" | while IFS= read -r status; do
    case "$status" in
        removed:*) info "Removed ${status#removed:} langfuse hook entries from $CURSOR_HOOKS_JSON" ;;
        not_found) info "No langfuse hook entries found in $CURSOR_HOOKS_JSON (already removed)" ;;
        invalid) warn "hooks.json is not a valid JSON object, skipping" ;;
    esac
done
else
    info "hooks.json not found: $CURSOR_HOOKS_JSON (skipping)"
fi

# --- 3. Remove env file ---
if [ "$PURGE" = true ]; then
    if [ -f "$LANGFUSE_ENV_FILE" ]; then
        rm -f "$LANGFUSE_ENV_FILE"
        info "Removed env file: $LANGFUSE_ENV_FILE"
    else
        info "Env file not found: $LANGFUSE_ENV_FILE (skipping)"
    fi
else
    info "Env file preserved: $LANGFUSE_ENV_FILE"
fi

# --- 4. Clean up profile.d directory if empty ---
if [ -d "$LANGFUSE_PROFILE_DIR" ] && [ -z "$(ls -A "$LANGFUSE_PROFILE_DIR" 2>/dev/null)" ]; then
    rmdir "$LANGFUSE_PROFILE_DIR"
    info "Removed empty profile directory: $LANGFUSE_PROFILE_DIR"
fi

# --- 5. Remove cursor state files ---
STATE_DIR="$HOME/.agent-exporter-to-langfuse/data/cursor-sessions"
if [ -d "$STATE_DIR" ]; then
    rm -rf "$STATE_DIR"
    info "Removed cursor state directory: $STATE_DIR"
fi

echo ""
info "Uninstallation complete!"
echo ""
echo "  Restart Cursor to apply changes."
