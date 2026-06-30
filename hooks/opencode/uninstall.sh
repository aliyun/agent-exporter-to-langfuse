#!/usr/bin/env bash
set -euo pipefail

OC_CONFIG_DIR="$HOME/.config/opencode"
OC_CONFIG_FILE="$OC_CONFIG_DIR/opencode.json"
PLUGIN_FILE="$OC_CONFIG_DIR/plugins/langfuse-exporter.mjs"
LANGFUSE_PROFILE_DIR="$HOME/.agent-exporter-to-langfuse/config"
LANGFUSE_ENV_FILE="$LANGFUSE_PROFILE_DIR/opencode.env"
LOG_DIR="$OC_CONFIG_DIR/logs/langfuse-exporter"

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

PURGE=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        --purge) PURGE=true; shift ;;
        *) shift ;;
    esac
done

echo "=== Uninstall OpenCode Langfuse Exporter ==="
echo ""

# --- 1. Remove plugin from opencode.json ---
if [ -f "$OC_CONFIG_FILE" ]; then
    python3 -c "
import json, sys
config_path = sys.argv[1]
try:
    with open(config_path) as f:
        config = json.load(f)
except Exception:
    sys.exit(0)

plugins = config.get('plugin', [])
if not isinstance(plugins, list):
    sys.exit(0)

filtered = [p for p in plugins if 'langfuse-exporter' not in str(p)]
if len(filtered) < len(plugins):
    config['plugin'] = filtered
    with open(config_path, 'w') as f:
        json.dump(config, f, indent=2)
    print('removed')
else:
    print('not_found')
" "$OC_CONFIG_FILE" | while IFS= read -r line; do
        case "$line" in
            removed)   info "Removed plugin from $OC_CONFIG_FILE" ;;
            not_found) warn "Plugin not found in $OC_CONFIG_FILE" ;;
        esac
    done
else
    warn "Config file not found, skipping: $OC_CONFIG_FILE"
fi

# --- 2. Remove plugin file and langstash-deliver ---
if [ -f "$PLUGIN_FILE" ]; then
    rm -f "$PLUGIN_FILE"
    info "Removed plugin: $PLUGIN_FILE"
else
    warn "Plugin file not found, skipping: $PLUGIN_FILE"
fi

DELIVER_DIR="$OC_CONFIG_DIR/plugins/langstash-deliver"
if [ -d "$DELIVER_DIR" ]; then
    rm -rf "$DELIVER_DIR"
    info "Removed langstash-deliver: $DELIVER_DIR"
fi

# --- 3. Remove env file ---
if [ "$PURGE" = true ]; then
    if [ -f "$LANGFUSE_ENV_FILE" ]; then
        rm -f "$LANGFUSE_ENV_FILE"
        info "Removed env file: $LANGFUSE_ENV_FILE"
    else
        warn "Env file not found, skipping: $LANGFUSE_ENV_FILE"
    fi
else
    info "Env file preserved: $LANGFUSE_ENV_FILE"
fi

# --- 4. Clean up profile.d directory if empty ---
if [ -d "$LANGFUSE_PROFILE_DIR" ] && [ -z "$(ls -A "$LANGFUSE_PROFILE_DIR" 2>/dev/null)" ]; then
    rmdir "$LANGFUSE_PROFILE_DIR"
    info "Removed empty profile directory: $LANGFUSE_PROFILE_DIR"

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
        info "Removed loader line from $SHELL_RC."
    fi
fi

# --- 5. Remove log files ---
if [ -d "$LOG_DIR" ]; then
    rm -rf "$LOG_DIR"
    info "Removed log directory: $LOG_DIR"
fi

echo ""
info "Uninstall complete. Restart OpenCode to apply."
