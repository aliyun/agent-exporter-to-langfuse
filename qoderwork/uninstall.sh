#!/usr/bin/env bash
set -euo pipefail

HOOK_DIR="$HOME/.qoderwork/hooks/langfuse"
SETTINGS_FILE="$HOME/.qoderwork/settings.json"
LANGFUSE_ENV_FILE="$HOME/.qoderwork/langfuse.env"
LAUNCH_AGENT_PLIST="$HOME/Library/LaunchAgents/com.qoderwork.langfuse-env.plist"

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

echo "=== Uninstall QoderWork Langfuse Hook ==="
echo ""

# --- 1. Remove hook files ---
if [ -d "$HOOK_DIR" ]; then
    rm -rf "$HOOK_DIR"
    info "Removed hook directory: $HOOK_DIR"
else
    warn "Hook directory not found, skipping: $HOOK_DIR"
fi

# --- 2. Remove hook entry from settings.json ---
if [ -f "$SETTINGS_FILE" ]; then
    python3 -c "
import json, sys
settings_path = sys.argv[1]
try:
    with open(settings_path) as f:
        settings = json.load(f)
except Exception:
    sys.exit(0)

changed = False
for event in ['Stop', 'SubagentStop']:
    entries = settings.get('hooks', {}).get(event, [])
    filtered = [
        matcher for matcher in entries
        if not any('langfuse' in h.get('command', '') for h in matcher.get('hooks', []))
    ]
    if len(filtered) < len(entries):
        settings['hooks'][event] = filtered
        changed = True

if changed:
    with open(settings_path, 'w') as f:
        json.dump(settings, f, indent=2)
    print('removed')
else:
    print('not_found')
" "$SETTINGS_FILE"
    info "Removed Langfuse hook from $SETTINGS_FILE."
else
    warn "Settings file not found, skipping: $SETTINGS_FILE"
fi

# --- 3. Remove env file ---
if [ -f "$LANGFUSE_ENV_FILE" ]; then
    rm -f "$LANGFUSE_ENV_FILE"
    info "Removed env file: $LANGFUSE_ENV_FILE"
else
    warn "Env file not found, skipping: $LANGFUSE_ENV_FILE"
fi

# --- 4. Remove source line from shell profile ---
if [ -f "$SHELL_RC" ] && grep -qF "qoderwork/langfuse.env" "$SHELL_RC" 2>/dev/null; then
    python3 -c "
import sys
lines = open(sys.argv[1]).readlines()
out = []
skip_next = False
for line in lines:
    if '# Langfuse (QoderWork)' in line:
        skip_next = True
        if out and out[-1].strip() == '':
            out.pop()
        continue
    if skip_next and 'langfuse.env' in line:
        skip_next = False
        continue
    skip_next = False
    out.append(line)
open(sys.argv[1], 'w').writelines(out)
" "$SHELL_RC"
    info "Removed source line from $SHELL_RC."
fi

# --- 5. Remove LaunchAgent (macOS) ---
if [ "$(uname)" = "Darwin" ]; then
    if [ -f "$LAUNCH_AGENT_PLIST" ]; then
        launchctl unload "$LAUNCH_AGENT_PLIST" 2>/dev/null || true
        rm -f "$LAUNCH_AGENT_PLIST"
        info "Removed LaunchAgent: $LAUNCH_AGENT_PLIST"

        # Unset from current session
        launchctl unsetenv LANGFUSE_BASE_URL 2>/dev/null || true
        launchctl unsetenv LANGFUSE_PUBLIC_KEY 2>/dev/null || true
        launchctl unsetenv LANGFUSE_SECRET_KEY 2>/dev/null || true
    else
        warn "LaunchAgent not found, skipping."
    fi
fi

# --- 6. Remove state files ---
STATE_DIR="$HOME/.qoderwork/state"
removed_state=false
for f in "$STATE_DIR/langfuse_hook.log"* "$STATE_DIR/langfuse_state.json" "$STATE_DIR/langfuse_state.lock"; do
    if [ -f "$f" ]; then
        rm -f "$f"
        removed_state=true
    fi
done
if [ "$removed_state" = true ]; then
    info "Removed Langfuse state/log files from $STATE_DIR."
fi

echo ""
info "Uninstall complete. Restart QoderWork to apply."
