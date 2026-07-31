#!/usr/bin/env bash
set -euo pipefail

PI_HOME="$HOME/.pi"
PI_SETTINGS_FILE="$PI_HOME/agent/settings.json"
PI_HOOK_DIR="$PI_HOME/hooks/langfuse"
LANGFUSE_PROFILE_DIR="$HOME/.agent-exporter-to-langfuse/config"
LANGFUSE_ENV_FILE="$LANGFUSE_PROFILE_DIR/pi.env"

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

echo "=== Uninstall Pi Langfuse Exporter ==="
echo ""

# --- 1. Unregister the hook from Pi ---
STILL_REGISTERED=no
if [ -f "$PI_SETTINGS_FILE" ] && command -v python3 &>/dev/null; then
    STILL_REGISTERED=$(python3 -c "
import json, sys
from pathlib import Path

path = Path(sys.argv[1])
target = sys.argv[2]
try:
    data = json.loads(path.read_text())
except (OSError, ValueError):
    print('no')
    sys.exit(0)

packages = data.get('packages')
if not isinstance(packages, list):
    print('no')
    sys.exit(0)

print('yes' if any(str(entry) == target for entry in packages) else 'no')
" "$PI_SETTINGS_FILE" "$PI_HOOK_DIR")
fi

if [ "$STILL_REGISTERED" = "yes" ]; then
    if command -v pi &>/dev/null; then
        if pi remove "$PI_HOOK_DIR"; then
            info "Unregistered from Pi: $PI_HOOK_DIR"
        else
            remove_rc=$?
            warn "pi remove failed (exit code: $remove_rc); remove the entry manually from $PI_SETTINGS_FILE"
        fi
    else
        warn "Pi CLI not found; remove the entry for $PI_HOOK_DIR manually from $PI_SETTINGS_FILE"
    fi
else
    warn "Hook not registered in $PI_SETTINGS_FILE, skipping unregister"
fi

# --- 2. Remove the installed hook directory ---
if [ -d "$PI_HOOK_DIR" ]; then
    rm -rf "$PI_HOOK_DIR"
    info "Removed hook directory: $PI_HOOK_DIR"
else
    warn "Hook directory not found, skipping: $PI_HOOK_DIR"
fi

# --- 3. Remove env file only when purging ---
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

# --- 4. Clean up the config directory if empty ---
if [ -d "$LANGFUSE_PROFILE_DIR" ] && [ -z "$(ls -A "$LANGFUSE_PROFILE_DIR" 2>/dev/null)" ]; then
    rmdir "$LANGFUSE_PROFILE_DIR"
    info "Removed empty config directory: $LANGFUSE_PROFILE_DIR"
fi

echo ""
info "Uninstall complete. Restart Pi to apply."
echo "  Checkpoints (if any) remain in $HOME/.agent-exporter-to-langfuse/data/pi-checkpoints"
