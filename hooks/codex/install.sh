#!/usr/bin/env bash
set -euo pipefail

# Codex Langfuse Hook Installer
# Installs hook files to ~/.codex/hooks/langfuse/ and registers Stop hook in ~/.codex/hooks.json

CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
HOOKS_DIR="$CODEX_HOME/hooks"
LANGFUSE_HOOK_DIR="$HOOKS_DIR/langfuse"
CODEX_HOOKS_JSON="$CODEX_HOME/hooks.json"

# Shared config
LANGFUSE_PROFILE_DIR="$HOME/.agent-exporter-to-langfuse/config"
LANGFUSE_ENV_FILE="$LANGFUSE_PROFILE_DIR/codex.env"

# Shell profile
if [ -n "${ZSH_VERSION:-}" ] || [ "$(basename "${SHELL:-}")" = "zsh" ]; then
    SHELL_RC="$HOME/.zshenv"
else
    SHELL_RC="$HOME/.profile"
fi

LAUNCH_AGENT_DIR="$HOME/Library/LaunchAgents"
LAUNCH_AGENT_PLIST="$LAUNCH_AGENT_DIR/com.codex.langfuse-env.plist"

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

# --- Parse arguments ---
LANGFUSE_SECRET_KEY="${LANGFUSE_SECRET_KEY:-}"
LANGFUSE_PUBLIC_KEY="${LANGFUSE_PUBLIC_KEY:-}"
LANGFUSE_BASE_URL="${LANGFUSE_BASE_URL:-}"
LANGFUSE_USER_ID="${LANGFUSE_USER_ID:-}"
LANGFUSE_TAGS="${LANGFUSE_TAGS:-}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --secret-key)  LANGFUSE_SECRET_KEY="$2"; shift 2 ;;
        --public-key)  LANGFUSE_PUBLIC_KEY="$2"; shift 2 ;;
        --base-url)    LANGFUSE_BASE_URL="$2"; shift 2 ;;
        --user-id)     LANGFUSE_USER_ID="$2"; shift 2 ;;
        --tags)        LANGFUSE_TAGS="$2"; shift 2 ;;
        *) shift ;;
    esac
done

# --- 1. Check prerequisites ---
if ! command -v codex &>/dev/null && [ ! -d "$CODEX_HOME" ]; then
    error "Codex CLI is not installed. Install it first: https://github.com/openai/codex"
    exit 1
fi

if ! command -v uv &>/dev/null; then
    error "uv is not installed. Install it first: https://docs.astral.sh/uv/"
    exit 1
fi

# --- 2. Collect Langfuse credentials ---
if [ -n "$LANGFUSE_SECRET_KEY" ] && [ -n "$LANGFUSE_PUBLIC_KEY" ] && [ -n "$LANGFUSE_BASE_URL" ]; then
    :
else
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

    if [ -n "$LANGFUSE_USER_ID" ]; then
        prompt_input "Langfuse User ID [$LANGFUSE_USER_ID]: "
    else
        prompt_input "Langfuse User ID [default: OS username]: "
    fi
    read -r INPUT_USER_ID
    LANGFUSE_USER_ID="${INPUT_USER_ID:-$LANGFUSE_USER_ID}"

    if [ -n "$LANGFUSE_TAGS" ]; then
        prompt_input "Extra Tags [$LANGFUSE_TAGS]: "
    else
        prompt_input "Extra Tags (e.g. team:olap,env:prod) [none]: "
    fi
    read -r INPUT_TAGS
    LANGFUSE_TAGS="${INPUT_TAGS:-$LANGFUSE_TAGS}"

    echo ""
fi

# --- 3. Copy hook files ---
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$LANGFUSE_HOOK_DIR"

cp "$SCRIPT_DIR/hooks/langfuse_hook.py" "$LANGFUSE_HOOK_DIR/"
cp "$SCRIPT_DIR/hooks/langfuse-entrypoint.sh" "$LANGFUSE_HOOK_DIR/"
chmod +x "$LANGFUSE_HOOK_DIR/langfuse-entrypoint.sh"

info "Hook files copied to $LANGFUSE_HOOK_DIR"

# --- 4. Copy langstash-deliver library ---
DELIVER_SRC="$SCRIPT_DIR/../../hooks/langstash-deliver/python/langstash_deliver"
DELIVER_DST="$LANGFUSE_HOOK_DIR/langstash_deliver"

if [ -d "$DELIVER_SRC" ]; then
    rm -rf "$DELIVER_DST"
    cp -r "$DELIVER_SRC" "$DELIVER_DST"
    info "Copied langstash_deliver to $LANGFUSE_HOOK_DIR/"
else
    # Fallback: find from install dir
    INSTALL_DIR="$HOME/.agent-exporter-to-langfuse"
    DELIVER_SRC2="$INSTALL_DIR/hooks/langstash-deliver/python/langstash_deliver"
    if [ -d "$DELIVER_SRC2" ]; then
        rm -rf "$DELIVER_DST"
        cp -r "$DELIVER_SRC2" "$DELIVER_DST"
        info "Copied langstash_deliver from $INSTALL_DIR to $LANGFUSE_HOOK_DIR/"
    else
        warn "langstash_deliver source not found. Direct push will not be available."
    fi
fi

# --- 5. Initialize uv environment ---
if [ ! -f "$LANGFUSE_HOOK_DIR/pyproject.toml" ]; then
    info "Initializing uv environment in $LANGFUSE_HOOK_DIR ..."
    (cd "$LANGFUSE_HOOK_DIR" && uv init --name codex-langfuse-hook && uv add langfuse)
    info "uv environment ready."
else
    info "uv environment already exists, skipping init."
fi

# --- 6. Register Stop hook in ~/.codex/hooks.json ---
HOOK_COMMAND="bash \"~/.codex/hooks/langfuse/langfuse-entrypoint.sh\""

python3 -c "
import json, sys, os

hooks_json_path = sys.argv[1]
hook_command = sys.argv[2]

# Load or create hooks.json
if os.path.exists(hooks_json_path):
    with open(hooks_json_path, 'r') as f:
        hooks_data = json.load(f)
else:
    hooks_data = {'hooks': {}}

# Ensure hooks dict exists
if 'hooks' not in hooks_data:
    hooks_data['hooks'] = {}

# Ensure Stop array exists
if 'Stop' not in hooks_data['hooks']:
    hooks_data['hooks']['Stop'] = []

# Check if langfuse hook already exists
langfuse_entrypoint = os.path.dirname(hook_command.split('\"')[1])
hook_exists = False
for matcher in hooks_data['hooks']['Stop']:
    if 'hooks' in matcher:
        for hook in matcher['hooks']:
            if hook.get('type') == 'command' and langfuse_entrypoint in hook.get('command', ''):
                hook_exists = True
                break

# Add new hook entry if not exists
if not hook_exists:
    new_hook = {
        'hooks': [
            {
                'type': 'command',
                'command': hook_command,
                'timeout_ms': 30000
            }
        ]
    }
    hooks_data['hooks']['Stop'].append(new_hook)

    # Write back
    with open(hooks_json_path, 'w') as f:
        json.dump(hooks_data, f, indent=2)
        f.write('\n')

    print('added')
else:
    print('exists')
" "$CODEX_HOOKS_JSON" "$HOOK_COMMAND" | while IFS= read -r status; do
    case "$status" in
        added) info "Registered Stop hook in $CODEX_HOOKS_JSON" ;;
        exists) info "Stop hook already registered in $CODEX_HOOKS_JSON" ;;
    esac
done

# --- 7. Write env file ---
case ",$LANGFUSE_TAGS," in
    *,codex,*) FINAL_TAGS="$LANGFUSE_TAGS" ;;
    *) FINAL_TAGS="codex${LANGFUSE_TAGS:+,$LANGFUSE_TAGS}" ;;
esac

mkdir -p "$LANGFUSE_PROFILE_DIR"
{
    echo "export LANGFUSE_BASE_URL=\"$LANGFUSE_BASE_URL\""
    echo "export LANGFUSE_PUBLIC_KEY=\"$LANGFUSE_PUBLIC_KEY\""
    echo "export LANGFUSE_SECRET_KEY=\"$LANGFUSE_SECRET_KEY\""
    [ -n "$LANGFUSE_USER_ID" ] && echo "export LANGFUSE_USER_ID=\"$LANGFUSE_USER_ID\""
    echo "export LANGFUSE_TAGS=\"$FINAL_TAGS\""
    echo "export LANGSTASH_ENABLED=\"true\""
    echo "export LANGSTASH_URL=\"http://127.0.0.1:5288\""
} > "$LANGFUSE_ENV_FILE"
info "Env vars written to $LANGFUSE_ENV_FILE"

# --- 8. Add profile.d loader to shell profile ---
LOADER_LINE='for f in "$HOME"/.agent-exporter-to-langfuse/config/*.env; do [ -f "$f" ] && . "$f"; done'

if ! grep -qF "agent-exporter-to-langfuse" "$SHELL_RC" 2>/dev/null; then
    printf '\n# Agent Langfuse Exporters\n%s\n' "$LOADER_LINE" >> "$SHELL_RC"
    info "Added profile.d loader to $SHELL_RC"
else
    info "Profile.d loader already in $SHELL_RC"
fi

# --- 9. LaunchAgent for GUI apps (macOS only) ---
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
    <string>com.codex.langfuse-env</string>
    <key>RunAtLoad</key>
    <true/>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>-c</string>
        <string>
for f in "$HOME"/.agent-exporter-to-langfuse/config/*.env; do
    [ -f "$f" ] && . "$f"
done
env | grep -E '^(LANGFUSE_|LANGSTASH_)' | while IFS='=' read -r k v; do
    launchctl setenv "$k" "$v"
done
        </string>
    </array>
</dict>
</plist>
PLISTEOF

    launchctl load "$LAUNCH_AGENT_PLIST" 2>/dev/null || true
    . "$LANGFUSE_ENV_FILE"
    env | grep -E '^(LANGFUSE_|LANGSTASH_)' | while IFS='=' read -r k v; do
        launchctl setenv "$k" "$v"
    done

    info "LaunchAgent created at $LAUNCH_AGENT_PLIST"
fi

echo ""
info "Installation complete!"
echo ""
echo "  Langfuse Base URL:   $LANGFUSE_BASE_URL"
echo "  Langfuse Public Key: ${LANGFUSE_PUBLIC_KEY:0:12}..."
echo "  Langfuse Secret Key: ${LANGFUSE_SECRET_KEY:0:12}..."
echo "  Langfuse User ID:   ${LANGFUSE_USER_ID:-<OS username>}"
echo "  Langfuse Tags:      $FINAL_TAGS"
echo ""
echo "  Env file: $LANGFUSE_ENV_FILE"
echo "  Hook dir: $LANGFUSE_HOOK_DIR"
echo "  Restart Codex to start tracing."
