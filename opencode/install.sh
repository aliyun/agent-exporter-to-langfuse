#!/usr/bin/env bash
set -euo pipefail

OC_CONFIG_DIR="$HOME/.config/opencode"
OC_PLUGINS_DIR="$OC_CONFIG_DIR/plugins"
OC_CONFIG_FILE="$OC_CONFIG_DIR/opencode.json"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SOURCE_PLUGIN="$SCRIPT_DIR/hooks/langfuse-exporter.mjs"
PLUGIN_DEST="$OC_PLUGINS_DIR/langfuse-exporter.mjs"
PLUGIN_REF="./plugins/langfuse-exporter.mjs"
# Shell profile: zshenv for zsh, ~/.profile for others (bash/sh on Linux)
if [ -n "${ZSH_VERSION:-}" ] || [ "$(basename "${SHELL:-}")" = "zsh" ]; then
    SHELL_RC="$HOME/.zshenv"
else
    SHELL_RC="$HOME/.profile"
fi
LAUNCH_AGENT_DIR="$HOME/Library/LaunchAgents"
LAUNCH_AGENT_PLIST="$LAUNCH_AGENT_DIR/com.opencode.langfuse-env.plist"

# --- Colors ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }
prompt_input() { echo -ne "${CYAN}$1${NC}"; }

# --- 1. Check prerequisites ---
if ! command -v npm &>/dev/null; then
    error "npm is not installed. Install Node.js first: https://nodejs.org/"
    exit 1
fi

if [ ! -f "$SOURCE_PLUGIN" ]; then
    error "Source plugin not found: $SOURCE_PLUGIN"
    exit 1
fi

if [ ! -d "$OC_CONFIG_DIR" ]; then
    error "OpenCode config directory not found: $OC_CONFIG_DIR"
    error "Please install OpenCode first: https://opencode.ai"
    exit 1
fi

# --- 2. Collect Langfuse credentials ---

# Load existing env file so previous values become defaults
LANGFUSE_ENV_FILE="$OC_CONFIG_DIR/langfuse.env"
if [ -f "$LANGFUSE_ENV_FILE" ]; then
    # shellcheck disable=SC1090
    . "$LANGFUSE_ENV_FILE"
fi

echo ""
echo "=== Langfuse Configuration ==="
echo "Enter your Langfuse credentials (get them from Langfuse project settings → API Keys)."
echo ""

DEFAULT_BASE_URL="${LANGFUSE_BASE_URL:-https://us.cloud.langfuse.com}"
DEFAULT_PUBLIC_KEY="${LANGFUSE_PUBLIC_KEY:-}"
DEFAULT_SECRET_KEY="${LANGFUSE_SECRET_KEY:-}"

if [ -n "$DEFAULT_BASE_URL" ]; then
    prompt_input "Langfuse Base URL [$DEFAULT_BASE_URL]: "
else
    prompt_input "Langfuse Base URL: "
fi
read -r INPUT_BASE_URL
LANGFUSE_BASE_URL="${INPUT_BASE_URL:-$DEFAULT_BASE_URL}"

if [ -n "$DEFAULT_PUBLIC_KEY" ]; then
    prompt_input "Langfuse Public Key [${DEFAULT_PUBLIC_KEY:0:12}...]: "
else
    prompt_input "Langfuse Public Key (pk-lf-...): "
fi
read -r INPUT_PUBLIC_KEY
LANGFUSE_PUBLIC_KEY="${INPUT_PUBLIC_KEY:-$DEFAULT_PUBLIC_KEY}"

if [ -n "$DEFAULT_SECRET_KEY" ]; then
    prompt_input "Langfuse Secret Key [${DEFAULT_SECRET_KEY:0:12}...]: "
else
    prompt_input "Langfuse Secret Key (sk-lf-...): "
fi
read -r INPUT_SECRET_KEY
LANGFUSE_SECRET_KEY="${INPUT_SECRET_KEY:-$DEFAULT_SECRET_KEY}"

if [ -z "$LANGFUSE_PUBLIC_KEY" ] || [ -z "$LANGFUSE_SECRET_KEY" ]; then
    error "Public Key and Secret Key are required."
    exit 1
fi

echo ""
echo "=== Optional Settings (press Enter to skip) ==="
echo ""

DEFAULT_USER_ID="${OC_LANGFUSE_USER_ID:-}"
DEFAULT_TAGS="${OC_LANGFUSE_TAGS:-opencode}"
DEFAULT_MAX_CHARS="${OC_LANGFUSE_MAX_CHARS:-800000}"
DEFAULT_DEBUG="${OC_LANGFUSE_DEBUG:-true}"

prompt_input "User ID [${DEFAULT_USER_ID:-auto (OS username)}]: "
read -r INPUT_USER_ID
OC_LANGFUSE_USER_ID="${INPUT_USER_ID:-$DEFAULT_USER_ID}"

prompt_input "Tags, comma-separated [$DEFAULT_TAGS]: "
read -r INPUT_TAGS
OC_LANGFUSE_TAGS="${INPUT_TAGS:-$DEFAULT_TAGS}"

prompt_input "Max content chars [$DEFAULT_MAX_CHARS]: "
read -r INPUT_MAX_CHARS
OC_LANGFUSE_MAX_CHARS="${INPUT_MAX_CHARS:-$DEFAULT_MAX_CHARS}"

prompt_input "Debug logging (true/false) [$DEFAULT_DEBUG]: "
read -r INPUT_DEBUG
OC_LANGFUSE_DEBUG="${INPUT_DEBUG:-$DEFAULT_DEBUG}"

echo ""

# --- 3. Install langfuse npm package ---
info "Installing langfuse npm package in $OC_CONFIG_DIR ..."
(cd "$OC_CONFIG_DIR" && npm install langfuse --save 2>&1 | tail -1)
info "langfuse npm package installed."

# --- 4. Copy plugin file ---
mkdir -p "$OC_PLUGINS_DIR"
if [ -f "$PLUGIN_DEST" ]; then
    warn "Plugin already exists at $PLUGIN_DEST"
    read -rp "Overwrite? [y/N] " answer
    if [[ ! "$answer" =~ ^[Yy]$ ]]; then
        info "Skipped plugin copy."
    else
        cp "$SOURCE_PLUGIN" "$PLUGIN_DEST"
        info "Plugin updated."
    fi
else
    cp "$SOURCE_PLUGIN" "$PLUGIN_DEST"
    info "Plugin installed to $PLUGIN_DEST"
fi

# --- 5. Register plugin in opencode.json ---
if [ ! -f "$OC_CONFIG_FILE" ]; then
    echo '{}' > "$OC_CONFIG_FILE"
fi

python3 -c "
import json, sys

config_path = sys.argv[1]
plugin_ref = sys.argv[2]

with open(config_path) as f:
    config = json.load(f)

plugins = config.get('plugin', [])
if not isinstance(plugins, list):
    plugins = []

already = any('langfuse-exporter' in str(p) for p in plugins)
if already:
    print('skipped')
else:
    plugins.append(plugin_ref)
    config['plugin'] = plugins
    with open(config_path, 'w') as f:
        json.dump(config, f, indent=2)
    print('added')
" "$OC_CONFIG_FILE" "$PLUGIN_REF" | while IFS= read -r line; do
    case "$line" in
        added)   info "Registered plugin in $OC_CONFIG_FILE" ;;
        skipped) warn "Plugin already registered in $OC_CONFIG_FILE" ;;
    esac
done

# --- 6. Set environment variables (shell + GUI) ---
SOURCE_LINE="[ -f \"$LANGFUSE_ENV_FILE\" ] && . \"$LANGFUSE_ENV_FILE\""

# 6a. Write dedicated env file
{
    echo "export LANGFUSE_BASE_URL=\"$LANGFUSE_BASE_URL\""
    echo "export LANGFUSE_PUBLIC_KEY=\"$LANGFUSE_PUBLIC_KEY\""
    echo "export LANGFUSE_SECRET_KEY=\"$LANGFUSE_SECRET_KEY\""
    [ -n "$OC_LANGFUSE_USER_ID" ]   && echo "export OC_LANGFUSE_USER_ID=\"$OC_LANGFUSE_USER_ID\""
    [ "$OC_LANGFUSE_TAGS" != "opencode" ]    && echo "export OC_LANGFUSE_TAGS=\"$OC_LANGFUSE_TAGS\""
    [ "$OC_LANGFUSE_MAX_CHARS" != "800000" ] && echo "export OC_LANGFUSE_MAX_CHARS=\"$OC_LANGFUSE_MAX_CHARS\""
    [ "$OC_LANGFUSE_DEBUG" = "false" ]    && echo "export OC_LANGFUSE_DEBUG=\"false\""
} > "$LANGFUSE_ENV_FILE"
info "Env vars written to $LANGFUSE_ENV_FILE."

# 6b. Add source line to shell profile (idempotent)
if ! grep -qF "opencode/langfuse.env" "$SHELL_RC" 2>/dev/null; then
    printf '\n# Langfuse (OpenCode)\n%s\n' "$SOURCE_LINE" >> "$SHELL_RC"
    info "Added source line to $SHELL_RC."
else
    info "Source line already in $SHELL_RC, skipping."
fi

# 6c. LaunchAgent for GUI apps (macOS only)
if [ "$(uname)" = "Darwin" ]; then
    mkdir -p "$LAUNCH_AGENT_DIR"

    if [ -f "$LAUNCH_AGENT_PLIST" ]; then
        launchctl unload "$LAUNCH_AGENT_PLIST" 2>/dev/null || true
    fi

    cat > "$LAUNCH_AGENT_PLIST" << 'PLISTEOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.opencode.langfuse-env</string>
    <key>RunAtLoad</key>
    <true/>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>-c</string>
        <string>
if [ -f "$HOME/.config/opencode/langfuse.env" ]; then
    . "$HOME/.config/opencode/langfuse.env"
    env | grep -E '^(LANGFUSE_|OC_LANGFUSE_)' | while IFS='=' read -r k v; do
        launchctl setenv "$k" "$v"
    done
fi
        </string>
    </array>
</dict>
</plist>
PLISTEOF

    launchctl load "$LAUNCH_AGENT_PLIST" 2>/dev/null || true
    . "$LANGFUSE_ENV_FILE"
    env | grep -E '^(LANGFUSE_|OC_LANGFUSE_)' | while IFS='=' read -r k v; do
        launchctl setenv "$k" "$v"
    done

    info "LaunchAgent created at $LAUNCH_AGENT_PLIST (GUI)."
fi

echo ""
info "Installation complete!"
echo ""
echo "  Langfuse Base URL:   $LANGFUSE_BASE_URL"
echo "  Langfuse Public Key: ${LANGFUSE_PUBLIC_KEY:0:12}..."
echo "  Langfuse Secret Key: ${LANGFUSE_SECRET_KEY:0:12}..."
echo ""
echo "  Plugin: $PLUGIN_DEST"
echo "  Env file: $LANGFUSE_ENV_FILE"
echo "  Restart OpenCode to start tracing."
