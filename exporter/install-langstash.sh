#!/usr/bin/env bash
# Install langstash service. Called by install.sh after hook installation.
# Expects: INSTALL_DIR, LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_BASE_URL set by caller.
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-$HOME/.agent-exporter-to-langfuse}"
SCRIPT_SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
EXPORTER_DIR="${EXPORTER_DIR:-$SCRIPT_SELF_DIR}"
CONFIG_DIR="$INSTALL_DIR/config"
DATA_DIR="$INSTALL_DIR/data"
LOGS_DIR="$INSTALL_DIR/logs"

# --- Colors ---
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }

# --- 1. uv sync ---
if ! command -v uv &>/dev/null; then
    warn "uv not found, skipping langstash installation"
    return 0 2>/dev/null || exit 0
fi

info "Installing langstash dependencies ..."
UV_EXTRAS=""
if [ "$(uname)" = "Darwin" ]; then
    UV_EXTRAS="--extra macos"
fi
(cd "$EXPORTER_DIR" && uv sync $UV_EXTRAS 2>&1) || {
    warn "uv sync failed, langstash will not be available"
    return 0 2>/dev/null || exit 0
}

# --- 2. Write config.toml ---
CONFIG_FILE="$CONFIG_DIR/config.toml"
mkdir -p "$CONFIG_DIR" "$DATA_DIR/pending" "$DATA_DIR/failed" "$LOGS_DIR"

if [ ! -f "$CONFIG_FILE" ]; then
    cat > "$CONFIG_FILE" << EOF
[server]
host = "127.0.0.1"
port = 5288

[langfuse]
public_key = "${LANGFUSE_PUBLIC_KEY:-}"
secret_key = "${LANGFUSE_SECRET_KEY:-}"
base_url = "${LANGFUSE_BASE_URL:-https://us.cloud.langfuse.com}"

[storage]
data_dir = "$DATA_DIR"
max_size_gb = 20.0
retention_days = 30

[sender]
interval_seconds = 5
max_backoff_seconds = 300
batch_size = 1
timeout_seconds = 30
EOF
    info "Created $CONFIG_FILE"
else
    info "Config already exists: $CONFIG_FILE"
fi

# --- 3. Platform-specific service installation ---
LANGSTASH_BIN="$EXPORTER_DIR/.venv/bin/langstash"

if [ "$(uname)" = "Darwin" ]; then
    # macOS: launchd
    PLIST_DIR="$HOME/Library/LaunchAgents"
    PLIST_FILE="$PLIST_DIR/com.langstash.plist"
    mkdir -p "$PLIST_DIR"

    if [ -f "$PLIST_FILE" ]; then
        launchctl unload "$PLIST_FILE" 2>/dev/null || true
    fi

    cat > "$PLIST_FILE" << PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.langstash</string>
    <key>ProgramArguments</key>
    <array>
        <string>${LANGSTASH_BIN}</string>
        <string>run</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>5</integer>
    <key>StandardOutPath</key>
    <string>${LOGS_DIR}/langstash.log</string>
    <key>StandardErrorPath</key>
    <string>${LOGS_DIR}/langstash.err</string>
</dict>
</plist>
PLISTEOF

    launchctl load "$PLIST_FILE" 2>/dev/null || true
    info "LaunchAgent installed: $PLIST_FILE"

else
    # Linux: systemd user service
    SERVICE_DIR="$HOME/.config/systemd/user"
    SERVICE_FILE="$SERVICE_DIR/langstash.service"
    mkdir -p "$SERVICE_DIR"

    cat > "$SERVICE_FILE" << SVCEOF
[Unit]
Description=langstash - Agent Exporter to Langfuse

[Service]
ExecStart=${LANGSTASH_BIN} run --server-only
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
SVCEOF

    systemctl --user daemon-reload 2>/dev/null || warn "systemctl daemon-reload failed (is XDG_RUNTIME_DIR set?)"
    systemctl --user enable langstash 2>/dev/null || warn "systemctl enable failed"
    info "systemd service installed: $SERVICE_FILE"
fi

# --- 4. Print key configuration ---
echo ""
info "langstash configuration:"
echo "  Config:    $CONFIG_FILE"
echo "  Server:    http://127.0.0.1:5288"
echo "  Data:      $DATA_DIR"
echo "  Logs:      $LOGS_DIR"
echo "  Langfuse:  ${LANGFUSE_BASE_URL:-https://us.cloud.langfuse.com}"
