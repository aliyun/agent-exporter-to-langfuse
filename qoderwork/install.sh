#!/usr/bin/env bash
set -euo pipefail

HOOK_DIR="$HOME/.qoderwork/hooks/langfuse"
SETTINGS_FILE="$HOME/.qoderwork/settings.json"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SOURCE_HOOK="$SCRIPT_DIR/hooks/langfuse_hook.py"
SOURCE_RUNNER="$SCRIPT_DIR/hooks/langfuse-entrypoint.sh"
# Shell profile: zshenv for zsh, ~/.profile for others (bash/sh on Linux)
if [ -n "${ZSH_VERSION:-}" ] || [ "$(basename "${SHELL:-}")" = "zsh" ]; then
    SHELL_RC="$HOME/.zshenv"
else
    SHELL_RC="$HOME/.profile"
fi
LAUNCH_AGENT_DIR="$HOME/Library/LaunchAgents"
LAUNCH_AGENT_PLIST="$LAUNCH_AGENT_DIR/com.qoderwork.langfuse-env.plist"

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
if ! command -v uv &>/dev/null; then
    error "uv is not installed. Install it first: https://docs.astral.sh/uv/"
    exit 1
fi

if [ ! -f "$SOURCE_HOOK" ]; then
    error "Source hook not found: $SOURCE_HOOK"
    exit 1
fi

# --- 2. Collect Langfuse credentials ---

# Load existing env file so previous values become defaults
LANGFUSE_ENV_FILE="$HOME/.qoderwork/langfuse.env"
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

DEFAULT_USER_ID="${QDW_LANGFUSE_USER_ID:-}"
DEFAULT_TAGS="${QDW_LANGFUSE_TAGS:-qoderwork}"
DEFAULT_MAX_CHARS="${QDW_LANGFUSE_MAX_CHARS:-800000}"
DEFAULT_DEBUG="${QDW_LANGFUSE_DEBUG:-true}"

prompt_input "User ID [${DEFAULT_USER_ID:-auto (QoderWork user.name > OS username)}]: "
read -r INPUT_USER_ID
QDW_LANGFUSE_USER_ID="${INPUT_USER_ID:-$DEFAULT_USER_ID}"

prompt_input "Tags, comma-separated [$DEFAULT_TAGS]: "
read -r INPUT_TAGS
QDW_LANGFUSE_TAGS="${INPUT_TAGS:-$DEFAULT_TAGS}"

prompt_input "Max content chars [$DEFAULT_MAX_CHARS]: "
read -r INPUT_MAX_CHARS
QDW_LANGFUSE_MAX_CHARS="${INPUT_MAX_CHARS:-$DEFAULT_MAX_CHARS}"

prompt_input "Debug logging (true/false) [$DEFAULT_DEBUG]: "
read -r INPUT_DEBUG
QDW_LANGFUSE_DEBUG="${INPUT_DEBUG:-$DEFAULT_DEBUG}"

echo ""

# --- 3. Install hook script ---
if [ -f "$HOOK_DIR/langfuse_hook.py" ]; then
    warn "Hook script already exists at $HOOK_DIR/langfuse_hook.py"
    read -rp "Overwrite? [y/N] " answer
    if [[ ! "$answer" =~ ^[Yy]$ ]]; then
        info "Skipped hook script copy."
    else
        cp "$SOURCE_HOOK" "$HOOK_DIR/langfuse_hook.py"
        cp "$SOURCE_RUNNER" "$HOOK_DIR/langfuse-entrypoint.sh"
        chmod +x "$HOOK_DIR/langfuse-entrypoint.sh"
        info "Hook script updated."
    fi
else
    mkdir -p "$HOOK_DIR"
    cp "$SOURCE_HOOK" "$HOOK_DIR/langfuse_hook.py"
    cp "$SOURCE_RUNNER" "$HOOK_DIR/langfuse-entrypoint.sh"
    chmod +x "$HOOK_DIR/langfuse-entrypoint.sh"
    info "Hook script installed to $HOOK_DIR/"
fi

# --- 4. Initialize uv environment ---
if [ ! -f "$HOOK_DIR/pyproject.toml" ]; then
    info "Initializing uv environment in $HOOK_DIR ..."
    (cd "$HOOK_DIR" && uv init --name qoderwork-langfuse && uv add langfuse)
    info "uv environment ready."
else
    info "uv environment already exists, skipping init."
fi

# --- 5. Configure settings.json ---
HOOK_COMMAND="~/.qoderwork/hooks/langfuse/langfuse-entrypoint.sh"

mkdir -p "$(dirname "$SETTINGS_FILE")"
if [ ! -f "$SETTINGS_FILE" ]; then
    echo '{}' > "$SETTINGS_FILE"
fi

python3 -c "
import json, sys

settings_path = sys.argv[2]
hook_command = sys.argv[1]
hook_events = ['Stop', 'SubagentStop']

with open(settings_path) as f:
    settings = json.load(f)

if 'hooks' not in settings:
    settings['hooks'] = {}

added = []
skipped = []
for event in hook_events:
    if event not in settings['hooks']:
        settings['hooks'][event] = []
    already = any(
        'langfuse' in h.get('command', '')
        for matcher in settings['hooks'][event]
        for h in matcher.get('hooks', [])
    )
    if already:
        skipped.append(event)
    else:
        settings['hooks'][event].append({
            'hooks': [
                {
                    'type': 'command',
                    'command': hook_command,
                    'timeout': 20
                }
            ]
        })
        added.append(event)

with open(settings_path, 'w') as f:
    json.dump(settings, f, indent=2)

if added:
    print('added:' + ','.join(added))
if skipped:
    print('skipped:' + ','.join(skipped))
" "$HOOK_COMMAND" "$SETTINGS_FILE" | while IFS= read -r line; do
    case "$line" in
        added:*)   info "Added Langfuse hooks to $SETTINGS_FILE: ${line#added:}" ;;
        skipped:*) warn "Already configured in $SETTINGS_FILE: ${line#skipped:}" ;;
    esac
done

# --- 6. Set environment variables (shell + GUI) ---
SOURCE_LINE="[ -f \"$LANGFUSE_ENV_FILE\" ] && . \"$LANGFUSE_ENV_FILE\""

# 6a. Write dedicated env file
{
    echo "export LANGFUSE_BASE_URL=\"$LANGFUSE_BASE_URL\""
    echo "export LANGFUSE_PUBLIC_KEY=\"$LANGFUSE_PUBLIC_KEY\""
    echo "export LANGFUSE_SECRET_KEY=\"$LANGFUSE_SECRET_KEY\""
    [ -n "$QDW_LANGFUSE_USER_ID" ]   && echo "export QDW_LANGFUSE_USER_ID=\"$QDW_LANGFUSE_USER_ID\""
    [ "$QDW_LANGFUSE_TAGS" != "qoderwork" ]    && echo "export QDW_LANGFUSE_TAGS=\"$QDW_LANGFUSE_TAGS\""
    [ "$QDW_LANGFUSE_MAX_CHARS" != "800000" ] && echo "export QDW_LANGFUSE_MAX_CHARS=\"$QDW_LANGFUSE_MAX_CHARS\""
    [ "$QDW_LANGFUSE_DEBUG" = "false" ]    && echo "export QDW_LANGFUSE_DEBUG=\"false\""
} > "$LANGFUSE_ENV_FILE"
info "Env vars written to $LANGFUSE_ENV_FILE."

# 6b. Add source line to shell profile (idempotent)
if ! grep -qF "qoderwork/langfuse.env" "$SHELL_RC" 2>/dev/null; then
    printf '\n# Langfuse (QoderWork)\n%s\n' "$SOURCE_LINE" >> "$SHELL_RC"
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
    <string>com.qoderwork.langfuse-env</string>
    <key>RunAtLoad</key>
    <true/>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>-c</string>
        <string>
if [ -f "$HOME/.qoderwork/langfuse.env" ]; then
    . "$HOME/.qoderwork/langfuse.env"
    env | grep -E '^(LANGFUSE_|QDW_LANGFUSE_)' | while IFS='=' read -r k v; do
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
    env | grep -E '^(LANGFUSE_|QDW_LANGFUSE_)' | while IFS='=' read -r k v; do
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
echo "  Env file: $LANGFUSE_ENV_FILE"
echo "  Restart QoderWork to start tracing."
