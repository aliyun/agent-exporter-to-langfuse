#!/usr/bin/env bash
set -euo pipefail

OC_CONFIG_DIR="$HOME/.config/opencode"
OC_PLUGINS_DIR="$OC_CONFIG_DIR/plugins"
OC_CONFIG_FILE="$OC_CONFIG_DIR/opencode.json"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SOURCE_PLUGIN="$SCRIPT_DIR/hooks/langfuse-exporter.mjs"
PLUGIN_DEST="$OC_PLUGINS_DIR/langfuse-exporter.mjs"
PLUGIN_REF="./plugins/langfuse-exporter.mjs"
# Shared env directory
LANGFUSE_PROFILE_DIR="$HOME/.agent-exporter-to-langfuse/config"
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
AUTO_YES=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --secret-key)  LANGFUSE_SECRET_KEY="$2"; shift 2 ;;
        --public-key)  LANGFUSE_PUBLIC_KEY="$2"; shift 2 ;;
        --base-url)    LANGFUSE_BASE_URL="$2"; shift 2 ;;
        --user-id)     LANGFUSE_USER_ID="$2"; shift 2 ;;
        --tags)        LANGFUSE_TAGS="$2"; shift 2 ;;
        -y|--yes|--upgrade) AUTO_YES=true; shift ;;
        *) shift ;;
    esac
done

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
LANGFUSE_ENV_FILE="$LANGFUSE_PROFILE_DIR/opencode.env"

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
fi

echo ""

# --- 3. Copy plugin file and langstash-deliver ---
mkdir -p "$OC_PLUGINS_DIR"
DELIVER_SRC="$SCRIPT_DIR/../langstash-deliver/typescript/dist"
DELIVER_DEST="$OC_PLUGINS_DIR/langstash-deliver"

if [ -f "$PLUGIN_DEST" ]; then
    if [ "$AUTO_YES" = true ]; then
        cp "$SOURCE_PLUGIN" "$PLUGIN_DEST"
        info "Plugin updated."
    else
        warn "Plugin already exists at $PLUGIN_DEST"
        read -rp "Overwrite? [y/N] " answer
        if [[ ! "$answer" =~ ^[Yy]$ ]]; then
            info "Skipped plugin copy."
        else
            cp "$SOURCE_PLUGIN" "$PLUGIN_DEST"
            info "Plugin updated."
        fi
    fi
else
    cp "$SOURCE_PLUGIN" "$PLUGIN_DEST"
    info "Plugin installed to $PLUGIN_DEST"
fi

if [ -d "$DELIVER_SRC" ]; then
    mkdir -p "$DELIVER_DEST"
    cp "$DELIVER_SRC"/index.js "$DELIVER_DEST/" 2>/dev/null || true
    cp "$DELIVER_SRC"/index.d.ts "$DELIVER_DEST/" 2>/dev/null || true
    info "langstash-deliver installed to $DELIVER_DEST"
else
    warn "langstash-deliver dist not found at $DELIVER_SRC, skipping"
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

# --- 6. Write env file (standalone) ---
# Build final tags: ensure agent name is included exactly once
case ",$LANGFUSE_TAGS," in
    *,opencode,*) FINAL_TAGS="$LANGFUSE_TAGS" ;;
    *) FINAL_TAGS="opencode${LANGFUSE_TAGS:+,$LANGFUSE_TAGS}" ;;
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
info "Env vars written to $LANGFUSE_ENV_FILE."

echo ""
info "Installation complete!"
echo ""
echo "  Langfuse Base URL:   $LANGFUSE_BASE_URL"
echo "  Langfuse Public Key: ${LANGFUSE_PUBLIC_KEY:0:12}..."
echo "  Langfuse Secret Key: ${LANGFUSE_SECRET_KEY:0:12}..."
echo "  Langfuse User ID:   ${LANGFUSE_USER_ID:-<OS username>}"
echo "  Langfuse Tags:      $FINAL_TAGS"
echo ""
echo "  Plugin: $PLUGIN_DEST"
echo "  Env file: $LANGFUSE_ENV_FILE"
echo "  Restart OpenCode to start tracing."
