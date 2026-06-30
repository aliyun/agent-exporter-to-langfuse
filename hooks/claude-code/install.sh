#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLUGIN_DIR="$SCRIPT_DIR"

# Shared env directory — each agent has its own .env file here
LANGFUSE_PROFILE_DIR="$HOME/.agent-exporter-to-langfuse/config"
LANGFUSE_ENV_FILE="$LANGFUSE_PROFILE_DIR/claude-code.env"

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
UPGRADE_MODE=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --secret-key)  LANGFUSE_SECRET_KEY="$2"; shift 2 ;;
        --public-key)  LANGFUSE_PUBLIC_KEY="$2"; shift 2 ;;
        --base-url)    LANGFUSE_BASE_URL="$2"; shift 2 ;;
        --user-id)     LANGFUSE_USER_ID="$2"; shift 2 ;;
        --tags)        LANGFUSE_TAGS="$2"; shift 2 ;;
        --upgrade)     UPGRADE_MODE=true; shift ;;
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
elif [ "$UPGRADE_MODE" = true ] && [ -f "$LANGFUSE_ENV_FILE" ]; then
    # shellcheck disable=SC1090
    . "$LANGFUSE_ENV_FILE"
    if [ -n "$LANGFUSE_SECRET_KEY" ] && [ -n "$LANGFUSE_PUBLIC_KEY" ] && [ -n "$LANGFUSE_BASE_URL" ]; then
        info "Upgrade mode: loaded credentials from $LANGFUSE_ENV_FILE"
    else
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

# --- 2.5. Copy langstash-deliver into hooks dir ---
LANGSTASH_SRC="$SCRIPT_DIR/../langstash-deliver/python/langstash_deliver"
LANGSTASH_DST="$PLUGIN_DIR/hooks/langstash_deliver"
if [ -d "$LANGSTASH_SRC" ]; then
    rm -rf "$LANGSTASH_DST"
    cp -R "$LANGSTASH_SRC" "$LANGSTASH_DST"
    info "langstash-deliver copied to hooks dir"
else
    warn "langstash-deliver source not found at $LANGSTASH_SRC, skipping"
fi

# --- 2.6. Pre-install Python dependencies ---
info "Installing Python dependencies ..."
(cd "$PLUGIN_DIR/hooks" && uv sync 2>&1) || {
    error "uv sync failed in $PLUGIN_DIR/hooks"
    exit 1
}
info "Python dependencies installed."

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
