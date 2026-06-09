#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLUGIN_DIR="$SCRIPT_DIR"

# Shared env directory — each agent has its own .env file here
LANGFUSE_PROFILE_DIR="$HOME/.agent-exporter-to-langfuse/config"
LANGFUSE_ENV_FILE="$LANGFUSE_PROFILE_DIR/claude-code.env"

# Shell profile
if [ -n "${ZSH_VERSION:-}" ] || [ "$(basename "${SHELL:-}")" = "zsh" ]; then
    SHELL_RC="$HOME/.zshenv"
else
    SHELL_RC="$HOME/.profile"
fi
LAUNCH_AGENT_DIR="$HOME/Library/LaunchAgents"
LAUNCH_AGENT_PLIST="$LAUNCH_AGENT_DIR/com.claude-code.langfuse-env.plist"

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

# --- Parse arguments (supports non-interactive mode from unified installer) ---
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
if ! command -v claude &>/dev/null; then
    error "claude CLI is not installed. Install it first: https://docs.anthropic.com/en/docs/claude-code"
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
    # Load existing env file so previous values become defaults
    if [ -f "$LANGFUSE_ENV_FILE" ]; then
        # shellcheck disable=SC1090
        . "$LANGFUSE_ENV_FILE"
    fi

    echo ""
    echo "=== Langfuse Configuration ==="
    echo "Enter your Langfuse credentials (get them from Langfuse project settings → API Keys)."
    echo ""

    DEFAULT_BASE_URL="${LANGFUSE_BASE_URL:-${LANGFUSE_BASE_URL:-https://us.cloud.langfuse.com}}"
    DEFAULT_PUBLIC_KEY="${LANGFUSE_PUBLIC_KEY:-${LANGFUSE_PUBLIC_KEY:-}}"
    DEFAULT_SECRET_KEY="${LANGFUSE_SECRET_KEY:-${LANGFUSE_SECRET_KEY:-}}"

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

# --- 3. Detect marketplace name ---
MARKETPLACE_NAME=""
if claude plugin marketplace list 2>/dev/null | grep -q "$PLUGIN_DIR"; then
    MARKETPLACE_NAME=$(claude plugin marketplace list 2>/dev/null | grep -B1 "$PLUGIN_DIR" | head -1 | sed 's/.*❯ //;s/[[:space:]]*$//')
fi

if [ -z "$MARKETPLACE_NAME" ]; then
    info "Adding plugin marketplace ..."
    claude plugin marketplace add "$PLUGIN_DIR" 2>&1 || true
    MARKETPLACE_NAME=$(claude plugin marketplace list 2>/dev/null | grep -B1 "$PLUGIN_DIR" | head -1 | sed 's/.*❯ //;s/[[:space:]]*$//')
fi

if [ -z "$MARKETPLACE_NAME" ]; then
    error "Failed to register marketplace. Please run manually:"
    echo "  claude plugin marketplace add $PLUGIN_DIR"
    exit 1
fi

info "Marketplace registered: $MARKETPLACE_NAME"

# --- 4. Install plugin with credentials ---
PLUGIN_REF="langfuse@$MARKETPLACE_NAME"

# Uninstall first if already installed (to refresh config)
if claude plugin list 2>/dev/null | grep -q "langfuse@"; then
    info "Removing existing langfuse plugin ..."
    claude plugin uninstall "$PLUGIN_REF" 2>/dev/null || claude plugin uninstall langfuse 2>/dev/null || true
fi

info "Installing langfuse plugin ..."

# Build final tags: ensure agent name is included exactly once
case ",$LANGFUSE_TAGS," in
    *,claude-code,*) FINAL_TAGS="$LANGFUSE_TAGS" ;;
    *) FINAL_TAGS="claude-code${LANGFUSE_TAGS:+,$LANGFUSE_TAGS}" ;;
esac

# Build config args — required params
INSTALL_ARGS=(
    --config "LANGFUSE_SECRET_KEY=$LANGFUSE_SECRET_KEY"
    --config "LANGFUSE_PUBLIC_KEY=$LANGFUSE_PUBLIC_KEY"
    --config "LANGFUSE_BASE_URL=$LANGFUSE_BASE_URL"
)
# Optional params
[ -n "$LANGFUSE_USER_ID" ] && INSTALL_ARGS+=(--config "LANGFUSE_USER_ID=$LANGFUSE_USER_ID")
INSTALL_ARGS+=(--config "LANGFUSE_TAGS=$FINAL_TAGS")

claude plugin install "$PLUGIN_REF" "${INSTALL_ARGS[@]}" 2>&1 || {
    error "Plugin installation failed."
    exit 1
}

# --- 5. Write env file (standalone) ---
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

# --- 6. Add profile.d loader to shell profile (one-time, shared across all agents) ---
LOADER_LINE='for f in "$HOME"/.agent-exporter-to-langfuse/config/*.env; do [ -f "$f" ] && . "$f"; done'

if ! grep -qF "agent-exporter-to-langfuse" "$SHELL_RC" 2>/dev/null; then
    printf '\n# Agent Langfuse Exporters\n%s\n' "$LOADER_LINE" >> "$SHELL_RC"
    info "Added profile.d loader to $SHELL_RC"
else
    info "Profile.d loader already in $SHELL_RC"
fi

# --- 7. LaunchAgent for GUI apps (macOS only) ---
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
    <string>com.claude-code.langfuse-env</string>
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
env | grep -E '^(LANGFUSE_|LANGFUSE_)' | while IFS='=' read -r k v; do
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
echo "  Restart Claude Code to start tracing."
