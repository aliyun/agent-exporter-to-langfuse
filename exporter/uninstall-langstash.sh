#!/usr/bin/env bash
# Uninstall langstash service. Called by uninstall.sh.
set -euo pipefail

GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }

# --- macOS: launchd ---
PLIST_FILE="$HOME/Library/LaunchAgents/com.langstash.plist"
if [ -f "$PLIST_FILE" ]; then
    launchctl unload "$PLIST_FILE" 2>/dev/null || true
    rm -f "$PLIST_FILE"
    info "Removed LaunchAgent: $PLIST_FILE"
fi

# --- Linux: systemd ---
SERVICE_FILE="$HOME/.config/systemd/user/langstash.service"
if [ -f "$SERVICE_FILE" ]; then
    systemctl --user disable --now langstash 2>/dev/null || true
    rm -f "$SERVICE_FILE"
    systemctl --user daemon-reload 2>/dev/null || true
    info "Removed systemd service: $SERVICE_FILE"
fi

info "langstash service uninstalled."
