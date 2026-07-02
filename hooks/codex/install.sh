#!/usr/bin/env bash
set -euo pipefail

# Codex Langfuse Hook Installer
# Builds and installs JS hook to ~/.codex/hooks/langfuse/ and registers Stop hook in ~/.codex/hooks.json

CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
HOOKS_DIR="$CODEX_HOME/hooks"
LANGFUSE_HOOK_DIR="$HOOKS_DIR/langfuse"
CODEX_HOOKS_JSON="$CODEX_HOME/hooks.json"

# Shared config
LANGFUSE_PROFILE_DIR="$HOME/.agent-exporter-to-langfuse/config"
LANGFUSE_ENV_FILE="$LANGFUSE_PROFILE_DIR/codex.env"

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
if ! command -v codex &>/dev/null && [ ! -d "$CODEX_HOME" ]; then
    error "Codex CLI is not installed. Install it first: https://github.com/openai/codex"
    exit 1
fi

if ! command -v node &>/dev/null; then
    error "Node.js is not installed. Install Node.js >= 22: https://nodejs.org/"
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

# --- 3. Build and install hook ---
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Use pre-built dist from tarball if available; otherwise fall back to npm build.
if [ -f "$SCRIPT_DIR/dist/index.mjs" ]; then
    info "Using pre-built dist/index.mjs, skipping build ..."
else
    # Fallback: build via npm. Requires langstash-deliver dist for trace.ts import.
    LANGSTASH_DELIVER_DIST="$SCRIPT_DIR/../langstash-deliver/typescript/dist/index.js"
    if [ ! -f "$LANGSTASH_DELIVER_DIST" ]; then
        error "langstash-deliver dist not found at $LANGSTASH_DELIVER_DIST"
        error "Build it first: cd hooks/langstash-deliver/typescript/ && npm install --ignore-scripts && npm run build"
        exit 1
    fi

    info "Pre-built dist not found, building via npm ..."
    if (cd "$SCRIPT_DIR" && npm install --ignore-scripts && npm run build); then
        :
    else
        build_rc=$?
        rm -rf "$SCRIPT_DIR/node_modules"
        error "npm install or npm run build failed (exit code $build_rc)"
        exit 1
    fi
    rm -rf "$SCRIPT_DIR/node_modules"

    if [ ! -f "$SCRIPT_DIR/dist/index.mjs" ]; then
        error "Build completed but dist/index.mjs not found"
        exit 1
    fi
fi

mkdir -p "$LANGFUSE_HOOK_DIR/dist"
cp -f "$SCRIPT_DIR/dist/index.mjs" "$LANGFUSE_HOOK_DIR/dist/"

info "Hook built and copied to $LANGFUSE_HOOK_DIR"

# --- 4. Register Stop hook in ~/.codex/hooks.json ---
HOOK_COMMAND="node \"\$HOME/.codex/hooks/langfuse/dist/index.mjs\""

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
hook_exists = False
for matcher in hooks_data['hooks']['Stop']:
    if 'hooks' in matcher:
        for hook in matcher['hooks']:
            if hook.get('type') == 'command' and 'langfuse' in hook.get('command', ''):
                hook_exists = True
                break

# Add new hook entry if not exists
if not hook_exists:
    new_hook = {
        'hooks': [
            {
                'type': 'command',
                'command': hook_command,
                'timeout': 30,
                'statusMessage': 'Uploading Codex trace to Langfuse'
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

# --- 5. Write env file ---
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
