#!/usr/bin/env bash
set -euo pipefail

LANGFUSE_PROFILE_DIR="$HOME/.config/agent-exporter-to-langfuse"
LANGFUSE_ENV_FILE="$LANGFUSE_PROFILE_DIR/claude-code.env"
LAUNCH_AGENT_PLIST="$HOME/Library/LaunchAgents/com.claude-code.langfuse-env.plist"

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

echo "=== Uninstall Claude Code Langfuse Plugin ==="
echo ""

# --- 1. Uninstall plugin via claude CLI ---
if command -v claude &>/dev/null; then
    info "Uninstalling langfuse plugin ..."
    claude plugin uninstall langfuse 2>/dev/null || true
    info "Plugin uninstalled."
else
    warn "claude CLI not found. Please uninstall manually: claude plugin uninstall langfuse"
fi

# --- 2. Remove env file ---
if [ -f "$LANGFUSE_ENV_FILE" ]; then
    rm -f "$LANGFUSE_ENV_FILE"
    info "Removed env file: $LANGFUSE_ENV_FILE"
else
    warn "Env file not found, skipping: $LANGFUSE_ENV_FILE"
fi

# --- 3. Clean up profile.d directory if empty ---
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

# --- 4. Remove LaunchAgent (macOS) ---
if [ "$(uname)" = "Darwin" ]; then
    if [ -f "$LAUNCH_AGENT_PLIST" ]; then
        launchctl unload "$LAUNCH_AGENT_PLIST" 2>/dev/null || true
        rm -f "$LAUNCH_AGENT_PLIST"
        info "Removed LaunchAgent: $LAUNCH_AGENT_PLIST"

        launchctl unsetenv LANGFUSE_BASE_URL 2>/dev/null || true
        launchctl unsetenv LANGFUSE_PUBLIC_KEY 2>/dev/null || true
        launchctl unsetenv LANGFUSE_SECRET_KEY 2>/dev/null || true
    else
        warn "LaunchAgent not found, skipping."
    fi
fi

# --- 5. Remove state files ---
STATE_DIR="$HOME/.claude/state"
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
info "Uninstall complete. Restart Claude Code to apply."
